"""Synthetic user scenarios: no account, chat database, cloud service or ASR model.

Only reader/service boundaries are replaced. The Gateway and its public tool
routing, timeline normalization/pagination and image decoder are production code.
All names, IDs, messages and media in this file are invented test fixtures.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from unified_mcp.server import text_result


DAY = "2026-01-02"
WX_PERSON = "wxid_synthetic_alex"
WX_OTHER = "wxid_synthetic_other_alex"
WX_GROUP = "synthetic_study@chatroom"
QQ_PERSON = "u_synthetic_alex"
QQ_GROUP = "synthetic-group-42"
TZ = dt.timezone(dt.timedelta(hours=8))


def epoch(value):
    if isinstance(value, (int, float)) or str(value).isdigit():
        return int(value)
    parsed = dt.datetime.fromisoformat(value)
    return int(parsed.replace(tzinfo=parsed.tzinfo or TZ).timestamp())


def wx(mid, when, text, *, local=None, sender="wx-peer", outgoing=False,
       kind="text", **extra):
    stamp = epoch(when)
    return {"id": {"server_id_str": str(mid), "local_id": int(local or mid)},
            "create_time": stamp, "time_iso": dt.datetime.fromtimestamp(stamp, TZ).isoformat(),
            "sender_wxid": sender, "sender": "Alex" if sender != "self" else "Me",
            "is_from_me": outgoing, "kind": kind, "text": text, **extra}


def qq(mid, when, text, *, sender="qq-peer", direction="from_contact", kind="text", **extra):
    stamp = epoch(when)
    return {"msg_id": str(mid), "timestamp": stamp,
            "time": dt.datetime.fromtimestamp(stamp, TZ).isoformat(),
            "sender_uid": sender, "sender": "Alex" if sender != "self" else "Me",
            "direction": direction, "kind": kind, "text": text, **extra}


class SyntheticReaders:
    """A documented upstream contract with observable queries and fault injection."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.calls = []
        self.fail_source = None
        self.wait_started = asyncio.Event()
        self.wait_release = asyncio.Event()
        self.block_wechat = False
        self.closed = False
        self.image = folder / "synthetic-chart.png"
        self.sticker = folder / "synthetic-sticker.gif"
        self.broken = folder / "synthetic-broken.jpg"
        Image.new("RGB", (24, 16), "#5599aa").save(self.image)
        Image.new("RGB", (12, 12), "red").save(
            self.sticker, save_all=True,
            append_images=[Image.new("RGB", (12, 12), "blue")], duration=120, loop=0)
        self.broken.write_bytes(b"\xff\xd8\xff\xe1this-is-not-a-complete-image")
        self.rows = {
            ("wechat", WX_PERSON): [
                wx(1, "2026-01-01T23:59:59+08:00", "previous day"),
                wx(2, "2026-01-02T00:00:00+08:00", "start boundary"),
                wx(3, "2026-01-02T09:00:00+08:00", "deadline Friday"),
                wx(4, "2026-01-02T09:00:00+08:00", "Friday confirmed", sender="self", outgoing=True),
                wx(4, "2026-01-02T09:00:00+08:00", "related system record", local=40, kind="system"),
                wx(5, "2026-01-02T09:00:01+08:00", "", sender=None, outgoing=None),
                wx(6, "2026-01-02T10:00:00+08:00", "synthetic voice", kind="voice",
                   voice={"synthetic_transcript": "Bring the blue folder tomorrow", "transcript_status": "cached"}),
                wx(7, "2026-01-02T10:01:00+08:00", "synthetic photo", kind="image",
                   images=[{"path": str(self.image)}]),
                wx(8, "2026-01-02T10:02:00+08:00", "synthetic sticker", kind="sticker",
                   images=[{"path": str(self.sticker)}]),
                wx(9, "2026-01-02T10:03:00+08:00", "voice file missing", kind="voice",
                   voice={"status": "unavailable", "reason": "local_audio_missing"}),
                wx(10, "2026-01-02T23:59:59+08:00", "end boundary 👩🏽‍💻 中文"),
                wx(11, "2026-01-03T00:00:00+08:00", "next day"),
            ],
            ("wechat", WX_OTHER): [wx(21, "2026-01-02T09:00:00+08:00", "different person secret")],
            ("qq", QQ_PERSON): [
                qq(101, "2026-01-01T23:59:59+08:00", "qq previous day"),
                qq(102, "2026-01-02T00:00:00+08:00", "qq start boundary"),
                qq(103, "2026-01-02T09:00:00+08:00", "deadline Friday on QQ"),
                qq(104, "2026-01-02T23:59:59+08:00", "QQ day end", sender="self", direction="from_me"),
                qq(105, "2026-01-03T00:00:00+08:00", "qq next day"),
            ],
            ("wechat", WX_GROUP): [
                wx(201, "2026-01-02T08:00:00+08:00", "agenda", sender="member-a"),
                wx(202, "2026-01-02T08:00:00+08:00", "agreed", sender="member-b"),
                wx(203, "2026-01-02T08:00:01+08:00", "followup", sender="member-a"),
                wx(204, "2026-01-02T08:00:02+08:00", "notice", sender=None, outgoing=None, kind="system"),
                wx(205, "2026-01-02T08:00:03+08:00", "my reply", sender="self", outgoing=True),
            ],
            ("qq", QQ_GROUP): [
                qq(301, "2026-01-02T08:00:00+08:00", "group agenda", sender="qa", direction="from_member"),
                qq(302, "2026-01-02T08:00:00+08:00", "second member", sender="qb", direction="from_member"),
                qq(303, "2026-01-02T08:00:01+08:00", "another reply", sender="qa", direction="from_member"),
                qq(304, "2026-01-02T08:00:02+08:00", "unknown member", sender=None, direction="unknown"),
            ],
        }

    def native(self, source, name, args):
        self.calls.append((source, name, copy.deepcopy(args)))
        if self.fail_source == source:
            raise RuntimeError(f"synthetic {source} reader unavailable")
        if name in {"resolve_chat", "resolve_contact", "resolve_group"}:
            if args["query"] == "missing person":
                return {"candidates": []}
            if source == "wechat":
                return {"candidates": [{"username": WX_PERSON, "name": "Alex"},
                                       {"username": WX_OTHER, "name": "Alex"}]}
            return {"candidates": [{"uid": QQ_PERSON, "name": "Alex"}]}
        if name == "diagnose":
            return {"ready": True, "synthetic": True}
        chat = args.get("talker") or args.get("chat") or args.get("contact")
        if (source, chat) not in self.rows:
            raise LookupError("Unknown synthetic chat; resolve a stable ID first")
        if name == "group_members":
            return {"members": [{"wxid": "member-a", "name": "Alex"},
                                {"wxid": "member-b", "name": "Alex"},
                                {"wxid": "self", "name": "Me"}]}
        rows = copy.deepcopy(self.rows[source, chat])
        if args.get("after"):
            rows = [r for r in rows if r.get("create_time", r.get("timestamp")) >= epoch(args["after"])]
        if args.get("before"):
            stop = epoch(args["before"])
            if len(str(args["before"])) == 10 and not str(args["before"]).isdigit():
                stop += 86400
            rows = [r for r in rows if r.get("create_time", r.get("timestamp")) < stop]
        if args.get("keyword"):
            rows = [r for r in rows if args["keyword"] in r.get("text", "")]
        if args.get("sender"):
            rows = [r for r in rows if r.get("sender_wxid", r.get("sender_uid")) == args["sender"]]
        kind = args.get("kind_name") or args.get("kind") or args.get("type")
        if kind:
            rows = [r for r in rows if r.get("kind") == kind]
        if args.get("server_id_str"):
            rows = [r for r in rows if r.get("id", {}).get("server_id_str") == str(args["server_id_str"])]
        if args.get("local_id") is not None:
            rows = [r for r in rows if r.get("id", {}).get("local_id") == int(args["local_id"])]
        if args.get("msg_id") is not None:
            rows = [r for r in rows if r.get("msg_id") == str(args["msg_id"])]
        reverse = args.get("order", "desc") == "desc"
        rows.sort(key=lambda row: (row.get("create_time", row.get("timestamp")),
                                  int(row.get("msg_id") or row["id"]["local_id"])), reverse=reverse)
        offset, limit = args.get("offset", 0), args.get("limit", 100)
        chosen = rows[offset:offset + limit]
        return {"messages": chosen, "query": {"has_more": offset + limit < len(rows),
                                                "returned": len(chosen), "next_offset": offset + len(chosen)}}

    async def wechat_call(self, name, args):
        if self.block_wechat:
            self.wait_started.set()
            await self.wait_release.wait()
        return text_result(self.native("wechat", name, args))

    def wechat(self):
        async def close():
            self.closed = True
        return SimpleNamespace(call=self.wechat_call, close=close,
                               status=lambda: {"ready": True, "running": False, "synthetic": True})

    def qq(self):
        return SimpleNamespace(call=lambda name, args: self.native("qq", name, args),
                               legacy=SimpleNamespace(_VFS_CONNECTION=None))


class SyntheticVoice:
    """Fixed golden transcript verifies routing/preservation, not ASR accuracy."""
    def __init__(self):
        self.calls = 0

    async def enrich(self, payload, **_):
        self.calls += 1
        def walk(value):
            if isinstance(value, list):
                return [walk(v) for v in value]
            if not isinstance(value, dict):
                return value
            result = {k: walk(v) for k, v in value.items()}
            voice = result.get("voice", {})
            if voice.get("synthetic_transcript"):
                from unified_mcp.voice_transcription import attach_transcript
                return attach_transcript(result, {"status": "ok", "text": voice["synthetic_transcript"],
                                                  "automatic": True, "human_verified": False,
                                                  "cache_hit": True, "engine": "synthetic-golden-cache"})
            return result
        return walk(payload)

    async def close(self):
        pass


class SyntheticOCR:
    def __init__(self):
        self.status = "no_text"
        self.calls = []

    def read(self, path):
        self.calls.append(str(path))
        return {"status": self.status, "text": "", "engine": "synthetic-golden-result"}

    def close(self):
        pass


async def demonstrate():
    """Produce inspectable sample conversations through the real public tools."""
    from contextlib import ExitStack
    import tempfile
    from unittest.mock import patch
    from unified_mcp.server import Gateway

    with tempfile.TemporaryDirectory(prefix="wxqq-demo-") as folder, ExitStack() as stack:
        fixture = SyntheticReaders(Path(folder))
        stack.enter_context(patch("unified_mcp.server.WeChatBackend", return_value=fixture.wechat()))
        stack.enter_context(patch("unified_mcp.server.VoiceService", return_value=SyntheticVoice()))
        stack.enter_context(patch("unified_mcp.wechat_media.enrich_wechat_media", side_effect=lambda value, *_: value))
        gateway = Gateway()
        gateway.qq = fixture.qq()
        gateway.image_text = SyntheticOCR()
        reports = []

        async def one(request, tool, arguments):
            result = await gateway.call(tool, arguments)
            data = json.loads(next(b.text for b in result.content if b.type == "text"))
            reports.append({"user_request": request, "tool": tool, "arguments": arguments,
                            "is_error": bool(result.isError), "result": data,
                            "image_blocks": sum(b.type == "image" for b in result.content)})
            return data

        async def all_pages(request, arguments):
            messages, page_counts, failure = [], [], None
            options = dict(arguments)
            for _ in range(100):
                result = await gateway.call("unified_timeline", options)
                data = json.loads(next(b.text for b in result.content if b.type == "text"))
                if result.isError:
                    failure = data
                    break
                messages.extend(data["messages"])
                page_counts.append(data["returned"])
                if not data["has_more"]:
                    break
                options["cursor"] = data["next_cursor"]
            else:
                failure = {"error": "No terminal page within the synthetic test budget"}
            reports.append({"user_request": request, "tool": "unified_timeline", "arguments": arguments,
                            "result": {"complete": failure is None and not data.get("has_more", True),
                                       "total_messages": len(messages), "page_counts": page_counts,
                                       "messages": messages, "failure": failure}})

        try:
            await one("看看我和 Alex 的聊天，先确认是哪一个人", "unified_resolve_chat", {"query": "Alex"})
            await all_pages("只看这个人的2026年1月2日，按时间完整读完", {
                "wechat_chat": WX_PERSON, "date": DAY, "order": "asc", "limit": 3, "include_media": False})
            await all_pages("已经确认两个平台都是同一个人，把微信和QQ的全部本地历史合起来", {
                "wechat_chat": WX_PERSON, "qq_chat": QQ_PERSON, "order": "asc", "limit": 3, "include_media": False})
            await one("找截止日期那句话", "unified_search", {
                "wechat_chat": WX_PERSON, "qq_chat": QQ_PERSON, "date": DAY,
                "keyword": "deadline", "include_media": False})
            await one("看看这句话前1条、后3条", "unified_context", {
                "source": "wechat", "chat_id": WX_PERSON, "date": DAY, "message_id": "3",
                "before_count": 1, "after_count": 3, "include_media": False})
            await one("读一下这条语音", "unified_message", {
                "source": "wechat", "chat_id": WX_PERSON, "date": DAY, "message_id": "6",
                "include_media": True, "include_image_text": False})
            gateway.image_text.status = "unavailable"
            await one("OCR坏了也没关系，直接让我看这张图", "unified_read_image", {"path": str(fixture.image)})
            gateway.image_text.status = "no_text"
            await one("看看这个动态表情，说明你实际看到哪一部分", "unified_read_image", {"path": str(fixture.sticker)})
            await one("统计群里今天谁说了多少话，同名成员别混起来", "unified_group_stats", {
                "source": "wechat", "chat_id": WX_GROUP, "date": DAY})
            await one("先看QQ群的两条做初步统计，别当作完整结果", "unified_group_stats", {
                "source": "qq", "chat_id": QQ_GROUP, "date": DAY, "max_messages": 2})
            fixture.fail_source = "qq"
            await one("两个平台一起查，但是QQ临时不可用", "unified_timeline", {
                "wechat_chat": WX_PERSON, "qq_chat": QQ_PERSON, "date": DAY, "include_media": False})
        finally:
            await gateway.close()
        report = {"synthetic": True, "no_live_accounts": True,
                  "transcription_scope": "Fixed synthetic voice cache; this is not a real ASR accuracy measurement.",
                  "scenarios": reports}
        # Reports can be committed or shared without leaking a developer's home
        # or temporary directory. Image bytes and real account data are omitted.
        encoded = json.dumps(report, ensure_ascii=False)
        encoded = encoded.replace(json.dumps(folder)[1:-1], "<synthetic-media>")
        return json.loads(encoded)


def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Run invented user requests through the actual Gateway")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data = json.dumps(asyncio.run(demonstrate()), ensure_ascii=False, indent=2)
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(data + "\n")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(data)


if __name__ == "__main__":
    main()
