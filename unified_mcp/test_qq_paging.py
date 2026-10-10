"""Synthetic SQLite and multiprocess checks; never opens a real account."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from unified_mcp import qq_adapter as adapter
from unified_mcp import qq_paging as paging
from unified_mcp.qq_segments import parse_segments

legacy = adapter.legacy
TS = 1700000000


def varint(value):
    output = bytearray()
    while value >= 128:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def field(number, value):
    raw = value.encode() if isinstance(value, str) else value
    return varint((number << 3) | 2) + varint(len(raw)) + raw


def payload(text):
    return field(1, field(45101, text))


def make_database(root, count, kind="private", *, with_fts=True):
    table = {"private": "c2c", "group": "group", "discuss": "discuss"}[kind] + "_msg_table"
    columns = "[40001] INTEGER PRIMARY KEY, [40050] INTEGER, [40020] TEXT, [40021] TEXT, [40003] INTEGER, [40027] INTEGER"
    main = sqlite3.connect(root / "nt_msg.db")
    main.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns}, [40011] INTEGER, [40012] INTEGER, [40013] INTEGER, [40800] BLOB, [40900] BLOB, [40033] INTEGER, [40030] INTEGER)")
    main.execute(f"CREATE INDEX IF NOT EXISTS idx_{kind}_page ON {table} ([40050], [40003], [40001])")
    main.executemany(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ((i, TS + i // 5, "peer", "peer", i % 5, 123, 0, 1, 0, payload(f"测试消息 {i} 😀"), b"", 2, 1) for i in range(1, count + 1)))
    main.commit()
    main.close()
    if with_fts:
        db = {"private": "buddy_msg_fts", "group": "group_msg_fts", "discuss": "discuss_msg_fts"}[kind]
        con = sqlite3.connect(root / (db + ".db"))
        con.execute(f"CREATE TABLE {db} ({columns}, [41701] TEXT, [41702] TEXT, [41703] INTEGER, [41704] INTEGER)")
        con.execute(f"CREATE INDEX idx_fts_page ON {db} ([40050], [40003], [40001])")
        con.executemany(f"INSERT INTO {db} VALUES (?,?,?,?,?,?,?,?,?,?)",
                        ((i, TS + i // 5, "peer", "peer", i % 5, 123, f"测试消息 {i} 😀", "", 0, 1) for i in range(1, count + 1)))
        con.commit()
        con.close()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = ExitStack()
        self.patches.enter_context(patch.object(legacy, "DEFAULT_DB_ROOT", self.root))
        self.patches.enter_context(patch.object(legacy, "CACHE_DIR", self.root / "cache"))
        self.patches.enter_context(patch.object(legacy, "DEFAULT_DATA_ROOT", None))
        self.patches.enter_context(patch.object(adapter, "_self_identity", {"uid": "self"}))
        self.patches.enter_context(patch.object(legacy, "get_self_uin", return_value="1"))
        self.patches.enter_context(patch.object(adapter, "get_key", return_value="fixture"))
        self.patches.enter_context(patch.object(adapter, "open_db", side_effect=lambda root, name, key: sqlite3.connect((root / name).as_uri() + "?mode=ro", uri=True)))
        self.patches.enter_context(patch.object(legacy, "resolve_chat_entity", side_effect=self.resolve))
        self.patches.enter_context(patch.object(legacy, "load_group_member_map", return_value={"by_uid": {"peer": {"display_name": "Fixture Member"}}}))

    def tearDown(self):
        self.patches.close()
        self.temp.cleanup()

    @staticmethod
    def resolve(query, kind):
        if kind == "private":
            return {"uid": query, "uin": "2", "display_name": "Fixture Peer"}, kind
        if kind == "group":
            return {"group_id": query, "display_name": "Fixture Group"}, kind
        return {"discuss_id": query, "display_name": "Fixture Discuss"}, kind

    def call(self, name="messages", **args):
        return adapter.call(name, {"contact": "peer", "include_media": False, **args})


class PagingTests(Fixture):
    def test_standalone_handlers_and_schema_use_same_bounded_path(self):
        make_database(self.root, 30)
        with patch.object(legacy, "load_chat_records", side_effect=AssertionError("unbounded legacy path")):
            page = legacy.TOOL_HANDLERS["messages"]({"contact": "peer", "limit": 2, "include_media": False})
        self.assertEqual(page["query"]["pagination"], "keyset")
        self.assertEqual(len(page["messages"]), 2)
        schema = next(t for t in legacy.TOOLS if t["name"] == "messages")["inputSchema"]["properties"]
        self.assertIn("cursor", schema)
        self.assertIn("sender", schema)

    def test_keyset_all_same_second_forward_reverse_and_display(self):
        make_database(self.root, 37)
        for order in ("asc", "desc"):
            expected = list(range(1, 38))
            if order == "desc":
                expected.reverse()
            rows, cursor = [], None
            while True:
                page = self.call(order=order, display_order=order, limit=3, cursor=cursor)
                rows.extend(int(r["msg_id"]) for r in page["messages"])
                if not page["query"]["has_more"]:
                    self.assertEqual(page["query"]["total"], 37)
                    break
                self.assertIsNone(page["query"]["total"])
                cursor = page["query"]["next_cursor"]
            self.assertEqual(rows, expected)
        page = self.call("chat_timeline", limit=4)
        self.assertEqual([int(r["msg_id"]) for r in page["messages"]], [34, 35, 36, 37])
        following = self.call("chat_timeline", limit=4, cursor=page["query"]["next_cursor"])
        self.assertEqual([int(r["msg_id"]) for r in following["messages"]], [30, 31, 32, 33])

    def test_offset_compatibility(self):
        make_database(self.root, 300)
        page = self.call(order="asc", offset=270, limit=20)
        self.assertEqual(page["messages"][0]["msg_id"], "271")
        self.assertEqual(page["query"]["next_offset"], 290)
        last = self.call(order="asc", offset=page["query"]["next_offset"], limit=20)
        self.assertEqual(last["query"]["total"], 300)
        self.assertFalse(last["query"]["has_more"])
        empty = self.call(order="asc", offset=500)
        self.assertEqual(empty["query"]["total"], 300)

    def test_filtered_empty_sql_batches_do_not_end_search(self):
        make_database(self.root, 1100)
        con = sqlite3.connect(self.root / "nt_msg.db")
        for ident in (780, 1060):
            con.execute("UPDATE c2c_msg_table SET [40800]=? WHERE [40001]=?", (payload("rare-needle"), ident))
        con.commit()
        con.close()
        con = sqlite3.connect(self.root / "buddy_msg_fts.db")
        con.execute("UPDATE buddy_msg_fts SET [41701]='' WHERE [40001] IN (780,1060)")
        con.commit()
        con.close()
        page = self.call("search", keyword="rare-needle", order="asc", limit=1)
        self.assertEqual(page["messages"][0]["msg_id"], "780")
        self.assertTrue(page["query"]["has_more"])
        last = self.call("search", keyword="rare-needle", order="asc", limit=1, cursor=page["query"]["next_cursor"])
        self.assertEqual(last["messages"][0]["msg_id"], "1060")
        self.assertEqual(last["query"]["total"], 2)
        self.assertFalse(last["query"]["has_more"])

    def test_fts_only_is_retained_and_main_is_authoritative(self):
        make_database(self.root, 10)
        con = sqlite3.connect(self.root / "nt_msg.db")
        con.execute("DELETE FROM c2c_msg_table WHERE [40001]=7")
        con.commit()
        con.close()
        con = sqlite3.connect(self.root / "buddy_msg_fts.db")
        con.execute("UPDATE buddy_msg_fts SET [40050]=? WHERE [40001]=4", (TS + 500,))
        con.commit()
        con.close()
        page = self.call(order="asc")
        self.assertEqual([int(r["msg_id"]) for r in page["messages"]], list(range(1, 11)))
        self.assertEqual(page["messages"][6]["source"], "fts_only")
        self.assertNotIn("source", page["messages"][3])

    def test_group_and_discuss_and_sender_filters(self):
        for kind in ("group", "discuss"):
            make_database(self.root, 15, kind=kind)
            page = self.call(contact="123", chat_type=kind, sender="peer", kind_name="text", order="asc", limit=5)
            self.assertEqual(len(page["messages"]), 5)
            self.assertEqual(page["messages"][0]["direction"], "from_member")
            self.assertEqual(page["messages"][0]["chat_type"], kind)
            empty = self.call(contact="123", chat_type=kind, sender="missing")
            self.assertEqual(empty["messages"], [])
            self.assertEqual(empty["query"]["total"], 0)

    def test_scope_changes_and_bad_cursor_fail(self):
        make_database(self.root, 10)
        cursor = self.call(limit=1)["query"]["next_cursor"]
        for change in ({"contact": "other"}, {"sender": "peer"}, {"kind_name": "image"}, {"keyword": "test"}, {"order": "asc"}, {"before": "2025-01-01"}, {"offset": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.call(cursor=cursor, **change)
        with self.assertRaises(ValueError):
            self.call(cursor="@@bad@@")

    def test_time_pushdown_and_no_fts_fallback_warning(self):
        make_database(self.root, 20, with_fts=False)
        page = self.call(order="asc", after=str(TS + 1), before=str(TS + 3))
        self.assertEqual([int(r["msg_id"]) for r in page["messages"]], list(range(5, 15)))
        self.assertTrue(page["warnings"])
        self.assertEqual(page["messages"][0]["text_source"], "payload.45101")

    def test_primary_database_failure_not_empty(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.call()

    def test_fts_disappearing_mid_pagination_requires_restart(self):
        make_database(self.root, 10)
        cursor = self.call(limit=1)["query"]["next_cursor"]
        (self.root / "buddy_msg_fts.db").unlink()
        with self.assertRaisesRegex(RuntimeError, "availability changed"):
            self.call(cursor=cursor)

    def test_bad_payload_retains_identity_not_garbage_text(self):
        make_database(self.root, 1, with_fts=False)
        con = sqlite3.connect(self.root / "nt_msg.db")
        con.execute("UPDATE c2c_msg_table SET [40800]=?", (b"\xff\xff" + "这只是二进制线索".encode(),))
        con.commit()
        con.close()
        row = self.call(include_media=True)["messages"][0]
        self.assertEqual(row["text"], "")
        self.assertTrue(row["payload_parse"]["malformed"])
        self.assertTrue(row["media"]["text_hints"])
        self.assertEqual(row["msg_id"], "1")

    def test_stats_streams_and_recent_cache_obeys_limit(self):
        make_database(self.root, 600)
        stats = self.call("stats")
        self.assertEqual(stats["total_messages"], 600)
        self.assertLessEqual(stats["read_metrics"]["max_rows_in_sql_page"], paging.CHUNK)
        cache = self.call("cache_recent", limit=6)
        self.assertEqual(cache["cache"]["cached_total"], 6)
        self.assertEqual(cache["messages"][0]["msg_id"], "600")

    def test_recall_context_both_directions(self):
        make_database(self.root, 15)
        con = sqlite3.connect(self.root / "nt_msg.db")
        con.execute("UPDATE c2c_msg_table SET [40012]=4,[40011]=5 WHERE [40001]=8")
        con.commit()
        con.close()
        for order in ("asc", "desc"):
            result = self.call("recall_events", order=order, context_window=2, limit=1)
            event = result["events"][0]
            self.assertEqual(event["event"]["msg_id"], "8")
            self.assertEqual([r["msg_id"] for r in event["context"]["before"]], ["6", "7"])
            self.assertEqual([r["msg_id"] for r in event["context"]["after"]], ["9", "10"])
            self.assertFalse(event["cache_lookup"]["recovered_original"])


class ExportTests(Fixture):
    def test_native_export_default_media_does_not_treat_count_as_array(self):
        make_database(self.root, 3)
        result = legacy.TOOL_HANDLERS["export_messages"]({"contact": "peer", "path": str(self.root / "default.jsonl")})
        self.assertEqual(result["messages"], 3)

    def test_export_unicode_streaming_overwrite_and_limit(self):
        make_database(self.root, 520)
        path = self.root / "export.jsonl"
        result = self.call("export_messages", path=str(path))
        self.assertEqual(result["messages"], 520)
        self.assertTrue(result["complete"])
        text = path.read_text(encoding="utf-8")
        self.assertIn("😀", text)
        with self.assertRaises(FileExistsError):
            self.call("export_messages", path=str(path), limit=1)
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        result = self.call("export_messages", path=str(path), limit=2, overwrite=True)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)
        result = self.call("export_messages", path=str(self.root / "out.md"), format="markdown", limit=1)
        self.assertIn("导出消息数: 1", (self.root / "out.md").read_text(encoding="utf-8"))

    def test_export_failure_preserves_existing_and_removes_partial(self):
        make_database(self.root, 10)
        path = self.root / "keep.jsonl"
        path.write_text("keep", encoding="utf-8")
        def fail(_):
            yield {"text": "fixture", "msg_id": "1"}
            raise RuntimeError("synthetic read failure")
        with patch.object(paging.Reader, "records", fail), self.assertRaisesRegex(RuntimeError, "synthetic"):
            self.call("export_messages", path=str(path), overwrite=True)
        self.assertEqual(path.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_export_group_and_discuss(self):
        for kind in ("group", "discuss"):
            make_database(self.root, 11, kind=kind)
            result = self.call("export_messages", contact="123", chat_type=kind, path=str(self.root / (kind + ".jsonl")))
            self.assertEqual(result["messages"], 11)


class LargePagingTests(Fixture):
    def test_120000_rows_pages_are_bounded_and_do_not_scan_all_fts(self):
        make_database(self.root, 120000)
        tracemalloc.start()
        try:
            first = self.call(order="asc", limit=25)
            second = self.call(order="asc", limit=25, cursor=first["query"]["next_cursor"])
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(first["messages"][0]["msg_id"], "1")
        self.assertEqual(second["messages"][0]["msg_id"], "26")
        self.assertLessEqual(second["query"]["read_metrics"]["sql_pages"], 2)
        self.assertLessEqual(second["query"]["read_metrics"]["decoded_records"], 2 * paging.CHUNK)
        self.assertLess(peak, 12 * 1024 * 1024)
        # Move deep into the synthetic store using the same validated cursor form.
        state = paging.decode_cursor(second["query"]["next_cursor"], paging.fingerprint("peer", "private", {"order": "asc"}, self.root))
        deep = paging.encode_cursor(state["scope"], (TS + 100000 // 5, 100000 % 5, 100000), 100000, True)
        page = self.call(order="asc", limit=25, cursor=deep)
        self.assertEqual(page["messages"][0]["msg_id"], "100001")
        self.assertLessEqual(page["query"]["read_metrics"]["sql_pages"], 2)
        print(f"QQ synthetic 120000 rows: two-page Python allocation peak={peak / 1024 / 1024:.2f} MiB; max SQL batch={paging.CHUNK}")


class SegmentTests(unittest.TestCase):
    def test_ordered_duplicate_text_and_image_hints_preserved(self):
        name = "a" * 32 + ".jpg"
        blob = field(1, field(45101, "first\nline") + field(99, name) + field(45101, "first\nline") + field(99, name))
        result = parse_segments(blob)
        self.assertEqual([s["type"] for s in result["segments"]], ["text", "image_reference_hint", "text", "image_reference_hint"])
        self.assertEqual(legacy.extract_qq_payload_text(blob), "first\nline\nfirst\nline")
        self.assertEqual(result["payload_parse"]["status"], "partial")

    def test_unknown_and_truncated_not_claimed_as_text(self):
        self.assertEqual(parse_segments(field(88, "ordinary unknown bytes"))["segments"], [])
        result = parse_segments(field(45101, "x") * 20, max_fields=3)
        self.assertTrue(result["payload_parse"]["truncated"])
        self.assertEqual(len(result["segments"]), 3)


class CacheTests(Fixture):
    def test_cross_process_merge_no_lost_updates(self):
        worker = """
import sys, time
from pathlib import Path
import qq_mcp_server as q
q.CACHE_DIR = Path(sys.argv[1])
start = int(sys.argv[2])
for i in range(start, start + 15):
    q.merge_cached_records({'uid':'synthetic-peer'}, [{'msg_id':str(i), 'timestamp':i, 'text':'fixture', 'kind':'text'}])
    time.sleep(0.003)
"""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        workers = [subprocess.Popen([sys.executable, "-X", "utf8", "-c", worker, str(legacy.CACHE_DIR), str(start)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags) for start in (1, 16)]
        try:
            for proc in workers:
                out, err = proc.communicate(timeout=30)
                self.assertEqual(proc.returncode, 0, err.decode(errors="replace"))
        finally:
            for proc in workers:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        records = legacy.load_cached_entries({"uid": "synthetic-peer"})
        self.assertEqual({entry["record"]["msg_id"] for entry in records}, {str(i) for i in range(1, 31)})

    def test_atomic_failure_and_malformed_cache_preserved(self):
        contact = {"uid": "synthetic"}
        legacy.merge_cached_records(contact, [{"msg_id": "1", "text": "original"}])
        path = legacy.cache_path_for_contact(contact)
        before = path.read_bytes()
        with patch.object(legacy.os, "replace", side_effect=OSError("synthetic replace failure")), self.assertRaises(OSError):
            legacy.merge_cached_records(contact, [{"msg_id": "2", "text": "next"}])
        self.assertEqual(path.read_bytes(), before)
        path.write_text("malformed", encoding="utf-8")
        with self.assertRaises(ValueError):
            legacy.merge_cached_records(contact, [{"msg_id": "3", "text": "third"}])
        self.assertEqual(path.read_text(), "malformed")


if __name__ == "__main__":
    unittest.main()
