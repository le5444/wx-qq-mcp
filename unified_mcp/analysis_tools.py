"""Bounded, source-scoped exact lookup, context and factual group aggregates."""
from __future__ import annotations

import datetime as dt
from collections import Counter

from unified_mcp.timeline import TZ, date_scope, start_bound, end_bound, decoded_result, normal_message
from unified_mcp.read_contract import canonical_chat, page_source_complete, page_warnings, validate_record_chat
from unified_mcp.record_identity import split_record_id, matches_record_id, stable_raw, raw_fingerprint
from unified_mcp.enrichment_updates import merge_update
from unified_mcp.time_scope import message_timestamp


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
        self.pagination_complete, self.source_complete = False, True
        self.identity_complete = True
        self.warnings = []
        self.canonical = None

    @property
    def coverage(self):
        return {"complete": self.complete, "scanned": self.scanned, "limited": self.limited,
                "error": self.error, "pagination_complete": self.pagination_complete,
                "source_complete": self.source_complete, "warnings": self.warnings,
                "identity_complete": self.identity_complete,
                "scope": "locally synchronized records in requested range"}

    async def rows(self):
        a, offset, cursor = self.args, 0, None
        previous_time = None
        if a.get("after") and int(a["after"]) >= int(a["before"]):
            self.complete = True
            self.pagination_complete = True
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
                for warning in page_warnings(page):
                    if warning not in self.warnings:
                        self.warnings.append(warning)
                self.source_complete &= page_source_complete(page)
                if isinstance(page.get("coverage"), dict) and page["coverage"].get("identity_complete") is False:
                    self.identity_complete = False
                canonical = canonical_chat(source, page, chat, self.canonical, chat_type=a.get("chat_type") if source == "qq" else None)
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
                checked = []
                for row in rows:
                    if row.get("error"):
                        raise RuntimeError("Source record could not be read")
                    validate_record_chat(source, row, canonical, chat_type=a.get("chat_type") if source == "qq" else None)
                    item = normal_message(source, row, canonical)
                    if item["identity_status"] in {"unresolved", "conflict"}:
                        self.identity_complete = False
                    timestamp = message_timestamp(item.get("timestamp"))
                    if timestamp is None or (a.get("after") and int(timestamp) < int(a["after"])) or int(timestamp) >= int(a["before"]):
                        raise RuntimeError("Source record escaped requested time scope")
                    if previous_time is not None and ((self.order == "asc" and timestamp < previous_time) or
                                                      (self.order == "desc" and timestamp > previous_time)):
                        raise RuntimeError("Source records are not in requested time order")
                    previous_time = timestamp
                    checked.append(item)
                self.canonical = canonical
                # Validate the entire bounded page before publishing any row.
                for item in checked:
                    self.scanned += 1
                    yield item
                if not more:
                    self.pagination_complete = True
                    self.complete = self.source_complete
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
    for name, value in (("record_id", record_id), ("message_id", message_id)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(name + " must be a nonempty string")
    if (record_id is not None) == (message_id is not None):
        raise ValueError("Specify exactly one of record_id or message_id")
    if record_id:
        prefix = result["chat_id"] + ":"
        if result["source"] == "wechat":
            base, _ = split_record_id("wechat", result["chat_id"], record_id)
            # Only intersect the user's scope; a foreign date must stay empty.
            parts = base[len(prefix):].rsplit(":", 3)
            timestamp = int(parts[-2])
            result["after"] = str(max(int(result.get("after") or timestamp), timestamp))
            result["before"] = str(min(int(result["before"]), timestamp + 1))
    return result


def _same_source_record(original, candidate):
    """Compare source facts while allowing later derived media metadata.

Missing/changed original text is a change even when the earlier text was empty.
Display names and newly available media metadata are not immutable identity.
"""
    if any(original.get(key) != candidate.get(key) for key in
           ("source", "chat_id", "message_id", "timestamp", "sender_id", "direction", "kind")):
        return False
    old_raw, new_raw = stable_raw(original["original"]), stable_raw(candidate["original"])
    fields = ("text", "kind", "kind_name", "create_time", "msg_id", "timestamp", "seq", "type_code", "msg_code",
              "sender_wxid", "sender_uid", "sender_uin", "is_from_me", "direction")
    return not any(key in old_raw and new_raw.get(key) != old_raw[key] for key in fields)


def _merge_target_media(original, candidate):
    update = {**candidate, "record_id": original["record_id"]}
    return merge_update(original, update)


async def message(args, fetch):
    a = identity_scope(args)
    scan = Scan(a, fetch, bounded(args.get("max_scan"), 20000, 1000000, "max_scan"))
    matches = []
    async for row in scan.rows():
        if ((args.get("record_id") and matches_record_id(a["source"], row["chat_id"], row["original"], args["record_id"])) or
                (args.get("message_id") is not None and row["message_id"] == str(args["message_id"]))):
            matches.append(row)
    result = {"status": "partial", "coverage": scan.coverage, "warnings": list(scan.warnings)}
    if not scan.complete:
        if matches:
            result["candidates"] = matches
        return result
    if not matches:
        return {**result, "status": "not_found"}
    if len(matches) > 1:
        return {**result, "status": "ambiguous", "candidates": matches}
    selected = matches[0]
    if args.get("record_id") and selected["record_id"] != args["record_id"]:
        selected = {**selected, "base_record_id": selected["record_id"], "record_id": args["record_id"]}
    if args.get("include_media", True):
        rich_scope = {**a, "after": str(selected["timestamp"]), "before": str(selected["timestamp"] + 1)}
        rich = Scan(rich_scope, fetch, 20000, media=True)
        candidates = []
        async for candidate in rich.rows():
            if matches_record_id(a["source"], candidate["chat_id"], candidate["original"], selected["record_id"]):
                candidates.append(candidate)
        result["warnings"].extend(w for w in rich.warnings if w not in result["warnings"])
        if not rich.complete:
            result["warnings"].append("media lookup incomplete; original record preserved")
        elif len(candidates) != 1:
            result["warnings"].append("media lookup missing or ambiguous; original record preserved")
        else:
            candidate = candidates[0]
            if not _same_source_record(selected, candidate):
                result["warnings"].append("source message changed during media lookup; original record preserved")
            else:
                try:
                    selected = _merge_target_media(selected, candidate)
                except ValueError:
                    result["warnings"].append("media identity mismatch; original record preserved")
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
    original_target = located["message"]
    neighbors, coverage, verified_targets = {}, {}, []
    maximum = bounded(args.get("max_scan"), 20000, 1000000, "max_scan")
    for side, order, bound in (("before", "desc", "before"), ("after", "asc", "after")):
        selected, candidates = [], []
        seen, second_closed = False, False
        requested = counts[side + "_count"]
        window = dict(a)
        window[bound] = str(original_target["timestamp"] + (1 if side == "before" else 0))
        scan = Scan(window, fetch, maximum, order=order, media=args.get("include_media", True))
        if requested:
            async for row in scan.rows():
                if row["timestamp"] != original_target["timestamp"]:
                    second_closed = True
                if matches_record_id(a["source"], row["chat_id"], row["original"], original_target["record_id"]):
                    seen = True
                    # Two candidates already prove ambiguity. Keep the bounded
                    # evidence needed for the verdict, not an unbounded list.
                    if len(candidates) < 2:
                        candidates.append(row)
                elif seen and len(selected) < requested:
                    selected.append(row)
                # Filling the requested neighbors is not enough: another
                # target with the same native identity can follow in this same
                # second or on its next page. Observe a second boundary first.
                if second_closed and len(selected) >= requested:
                    break
        target_verified = not requested or (
            (second_closed or scan.pagination_complete) and len(candidates) == 1
            and _same_source_record(original_target, candidates[0]))
        done = not requested or (scan.source_complete and not scan.error and target_verified
                                and (len(selected) == requested or scan.complete))
        side_warnings = list(scan.warnings)
        if requested and not target_verified:
            side_warnings.append("context target missing, ambiguous, changed, or not fully verified; original target preserved")
        coverage[side] = {**scan.coverage, "complete": done, "target_verified": target_verified,
                          "target_second_complete": second_closed or scan.pagination_complete,
                          "warnings": side_warnings}
        neighbors[side] = list(reversed(selected)) if side == "before" else selected
        if requested and target_verified:
            verified_targets.append(candidates[0])
    complete = all(item["complete"] for item in coverage.values())
    target = original_target
    warnings = located.get("warnings", []) + [w for side in coverage.values() for w in side["warnings"]]
    if complete:
        before_identities = {(row["source"], row["chat_id"], row["record_id"], raw_fingerprint(row["original"]))
                             for row in neighbors["before"]}
        if any((row["source"], row["chat_id"], row["record_id"], raw_fingerprint(row["original"])) in before_identities
               for row in neighbors["after"]):
            complete = False
            warnings.append("context order changed between passes; the same source record appeared on both sides")
    if complete and args.get("include_media", True):
        try:
            for candidate in verified_targets:
                target = _merge_target_media(target, candidate)
        except ValueError:
            complete = False
            target = original_target
            warnings.append("context media identity changed between passes; original target preserved")
    if not complete:
        # These neighbors were positioned against a target whose consistency
        # was not established. Returning them as ordinary context is misleading.
        neighbors = {"before": [], "after": []}
        target = original_target
        if not warnings:
            warnings.append("context scan incomplete; original target preserved")
    return {"status": "ok" if complete else "partial", "target": target, **neighbors,
            "coverage": {"complete": complete, "sides": coverage,
                         "scanned": located["coverage"]["scanned"] + sum(c["scanned"] for c in coverage.values())},
            "warnings": warnings}


async def group_stats(args, fetch):
    a = scope(args, group=True)
    scan = Scan(a, fetch, bounded(args.get("max_messages"), 20000, 1000000, "max_messages"))
    senders, kinds, days, names = Counter(), Counter(), Counter(), {}
    unresolved = 0
    async for row in scan.rows():
        sender = row.get("sender_id") or None
        senders[sender] += 1
        unresolved += row.get("identity_status") in {"unresolved", "conflict"}
        if sender and row.get("sender_name"):
            names[sender] = row["sender_name"]
        kinds[row.get("kind") or "unknown"] += 1
        days[dt.datetime.fromtimestamp(row["timestamp"], TZ).date().isoformat()] += 1
    sender_rows = [{"sender_id": sender, "sender_name": names.get(sender), "count": count}
                   for sender, count in senders.most_common()]
    summary = {"total_messages": scan.scanned, "unknown_sender_count": senders.get(None, 0),
               "unresolved_identity_count": unresolved,
               "system_messages": kinds.get("system", 0), "senders": sender_rows,
               "by_kind": dict(kinds), "by_day": dict(sorted(days.items()))}
    return {"status": "ok" if scan.complete else "partial", "source": a["source"], "chat_id": scan.canonical or a["chat_id"],
            "summary": summary, "coverage": scan.coverage,
            "warnings": list(scan.warnings),
            "note": "Counts include separately labeled system records; display names are not identities. Topic or relationship analysis requires reading supporting messages."}


def tool_definitions():
    from mcp.types import Tool
    common = {"source": {"type": "string", "enum": ["wechat", "qq"]}, "chat_id": {"type": "string"},
              "chat_type": {"type": "string", "enum": ["private", "group", "discuss"]},
              "date": {"type": "string", "description": "Local +08:00 calendar date YYYY-MM-DD; conflicts with after/before"},
              "after": {"type": "string", "description": "Inclusive ISO/Unix start; a bare date means +08:00 midnight"},
              "before": {"type": "string", "description": "Exclusive ISO/Unix end; a bare YYYY-MM-DD includes that entire local day"}}
    exact = {**common, "record_id": {"type": "string"}, "message_id": {"type": "string"},
             "max_scan": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 20000},
             "include_media": {"type": "boolean", "default": True}, "include_image_text": {"type": "boolean", "default": True}}
    specs = [("unified_message", exact, "Find one record by record_id or native message_id in one resolved chat. Ambiguous IDs return candidates. Partial scans never prove absence."),
             ("unified_context", {**exact, **{k: {"type": "integer", "minimum": 0, "maximum": 50, "default": 10} for k in ("before_count", "after_count")}}, "Read before/after context around one exact record, preserving same-second neighbors and the requested time scope."),
             ("unified_group_stats", {**common, "max_messages": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 20000}}, "Count group messages by stable sender identity, type and day. Labels incomplete scans; does not infer sentiment or relationships.")]
    return [Tool(name=name, description=description, inputSchema={"type": "object", "properties": properties,
                "required": ["source", "chat_id"], "additionalProperties": False}) for name, properties, description in specs]
