import asyncio
import unittest
from unittest.mock import patch

from unified_mcp.timeline import merged_timeline, normal_message, end_bound, start_bound


def wx(i, ts=100):
    return {"id": {"server_id_str": str(i)}, "create_time": ts, "is_from_me": False,
            "sender_wxid": "other", "text": str(i), "images": [{"path": "image.png"}]}


def qq(i, ts=100):
    return {"msg_id": str(i), "timestamp": ts, "direction": "from_me", "sender_uid": "self", "text": str(i)}


class TimelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_wechat_stable_id_bypasses_name_resolution(self):
        async def fetch(source, name, args):
            self.assertEqual(source, "wechat")
            self.assertEqual(args["talker"], "wxid_verified_contact")
            self.assertNotIn("chat", args)
            return {"messages": [], "query": {"has_more": False}}
        result = await merged_timeline({"wechat_chat": "wxid_verified_contact"}, fetch)
        self.assertEqual(result["status"], "ok")

    async def get_all(self, direction):
        data = {"wechat": [wx(1, 100), wx(2, 100), wx(3, 102), wx(4, 104)],
                "qq": [qq(1, 100), qq(2, 101), qq(3, 104)]}
        async def fetch(source, name, args):
            rows = sorted(data[source], key=lambda r: (r.get("create_time", r.get("timestamp")), int(r.get("msg_id") or r["id"]["server_id_str"])), reverse=args["order"] == "desc")
            offset, limit = args["offset"], args["limit"]
            return {"messages": rows[offset:offset + limit], "query": {"has_more": offset + limit < len(rows)}}
        args = {"wechat_chat": "w", "qq_chat": "q", "order": direction, "limit": 2}
        result = []
        for _ in range(10):
            page = await merged_timeline(args, fetch)
            self.assertEqual(page["status"], "ok")
            result += page["messages"]
            if not page["has_more"]:
                break
            args["cursor"] = page["next_cursor"]
        self.assertEqual(len(result), 7)
        self.assertEqual(len({(m["source"], m["message_id"]) for m in result}), 7)
        self.assertEqual([m["timestamp"] for m in result], sorted([m["timestamp"] for m in result], reverse=direction == "desc"))
        wx_ids = [m["message_id"] for m in result if m["source"] == "wechat"]
        self.assertEqual(wx_ids, ["4", "3", "2", "1"] if direction == "desc" else ["1", "2", "3", "4"])

    async def test_ascending_pagination_no_loss(self):
        await self.get_all("asc")

    async def test_descending_pagination_no_loss(self):
        await self.get_all("desc")

    async def test_partial_does_not_advance(self):
        async def fetch(source, *_):
            if source == "qq":
                raise RuntimeError("key unavailable")
            return {"messages": [wx(1)], "query": {}}
        page = await merged_timeline({"wechat_chat": "w", "qq_chat": "q"}, fetch)
        self.assertEqual(page["status"], "partial")
        self.assertIn("wechat", page["available_source_pages"])
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(page["messages"], [])

    async def test_cursor_cannot_cross_chats(self):
        async def fetch(*_):
            return {"messages": [wx(1), wx(2)], "query": {"has_more": True}}
        page = await merged_timeline({"wechat_chat": "w", "limit": 1}, fetch)
        with self.assertRaises(ValueError):
            await merged_timeline({"wechat_chat": "other", "limit": 1, "cursor": page["next_cursor"]}, fetch)

    async def test_missing_timestamp_is_error(self):
        async def fetch(*_):
            return {"messages": [{"text": "unreadable"}], "query": {}}
        page = await merged_timeline({"wechat_chat": "w"}, fetch)
        self.assertEqual(page["status"], "partial")

    async def test_empty_is_distinct_from_failure(self):
        async def fetch(*_):
            return {"messages": [], "query": {"has_more": False}}
        page = await merged_timeline({"qq_chat": "q"}, fetch)
        self.assertEqual(page["status"], "ok")
        self.assertFalse(page["has_more"])

    async def test_native_record_errors_not_hidden(self):
        async def fetch(*_):
            return {"messages": [{"error": "missing key"}]}
        page = await merged_timeline({"wechat_chat": "w"}, fetch)
        self.assertEqual(page["status"], "partial")

    def test_raw_media_preserved(self):
        self.assertEqual(normal_message("wechat", wx(1), "w")["original"]["images"][0]["path"], "image.png")

    def test_shared_wechat_server_id_preserves_distinct_local_records(self):
        first = {**wx(1), "id": {"server_id_str": "123", "local_id": 10}, "kind": "text"}
        second = {**first, "id": {"server_id_str": "123", "local_id": 11}, "kind": "system"}
        a, b = [normal_message("wechat", row, "w") for row in (first, second)]
        self.assertEqual(a["message_id"], b["message_id"])
        self.assertNotEqual(a["record_id"], b["record_id"])
        self.assertEqual(a["record_id"], normal_message("wechat", {**first, "images": []}, "w")["record_id"])

    def test_unknown_sender_not_assumed(self):
        self.assertEqual(normal_message("qq", {"msg_id": "1"}, "q")["direction"], "unknown")

    def test_timezone_bounds(self):
        self.assertEqual(start_bound("2025-01-02T03:04:05+08:00"), "1735758245")
        self.assertEqual(end_bound("2025-01-02", "2025-02-01T21:00:00+08:00"), "1735833600")
        self.assertEqual(end_bound("2026-01-01", "2025-01-02T03:04:05+08:00"), "1735758245")


class QQTests(unittest.TestCase):
    def setUp(self):
        from unified_mcp import qq_adapter
        self.a = qq_adapter

    def test_sender_uid_has_priority(self):
        with patch.object(self.a, "_self_identity", {"uid": "self"}), patch.object(self.a.legacy, "get_self_uin", return_value="1"):
            name, direction = self.a.sender_name({"uid": "peer", "uin": "2"}, "private", "self", {"sender_uin": "1"}, None)
            self.assertEqual(direction, "from_me")

    def test_incoming_not_me(self):
        with patch.object(self.a, "_self_identity", {"uid": "self"}), patch.object(self.a.legacy, "get_self_uin", return_value="1"):
            _, direction = self.a.sender_name({"uid": "peer", "uin": "2"}, "private", "peer", {"sender_uin": "2"}, None)
            self.assertEqual(direction, "from_contact")

    def test_unknown_not_automatically_me(self):
        with patch.object(self.a, "_self_identity", {"uid": "self"}), patch.object(self.a.legacy, "get_self_uin", return_value="1"):
            _, direction = self.a.sender_name({"uid": "peer", "uin": "2"}, "private", "", {}, None)
            self.assertEqual(direction, "unknown")

    def test_ambiguous_contact_rejected(self):
        with self.assertRaises(ValueError):
            self.a.require_unambiguous([{"uid": "a"}, {"uid": "b"}], "name")

    def test_correct_sender_column_and_time_pushdown(self):
        class Cursor:
            description = []
            def __iter__(self):
                return iter([])
        class Connection:
            def execute(self, sql, params):
                self.sql, self.params = sql, params
                return Cursor()
            def close(self):
                pass
        con = Connection()
        token = self.a.SCOPE.set({"after": "2026-09-23", "before": "2026-09-23"})
        try:
            with patch.object(self.a, "open_db", return_value=con), patch.object(self.a, "get_key", return_value="unused"):
                self.a.query_rows({"uid": "peer"}, "private", fts=False)
            self.assertIn("[40033] AS sender_uin", con.sql)
            self.assertIn("[40050] >= ? AND [40050] < ?", con.sql)
            self.assertEqual(con.params[-1], 1790179200)
        finally:
            self.a.SCOPE.reset(token)


if __name__ == "__main__":
    unittest.main()
