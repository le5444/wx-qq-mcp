"""Real SQLite identity regressions with invented people, numbers and messages."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

from unified_mcp import qq_adapter as adapter
from unified_mcp.qq_paging import Summary

legacy = adapter.legacy
SELF = ("u_self", 10001, "Fixture owner", "")
PEER = ("u_peer", 20001, "Fixture peer", "")


class IdentityFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        for obj, attr, value in ((legacy, "DEFAULT_DB_ROOT", self.root), (legacy, "DEFAULT_DATA_ROOT", None),
                                 (legacy, "CACHE_DIR", self.root / "cache"), (adapter, "_self_identity", None)):
            self.stack.enter_context(patch.object(obj, attr, value))
        self.stack.enter_context(patch.dict(os.environ, {"QQ_MCP_SELF_UIN": "10001", "QQ_MCP_SELF_UID": ""}))
        self.stack.enter_context(patch.object(legacy, "get_key", return_value="synthetic"))
        self.stack.enter_context(patch.object(adapter, "get_key", return_value="synthetic"))
        self.stack.enter_context(patch.object(legacy, "open_nt_db", side_effect=self.open))
        self.stack.enter_context(patch.object(adapter, "open_db", side_effect=self.open))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def open(self, root, name, key):
        path = (root / name).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise AssertionError("A synthetic test must not open a real account")
        return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)

    def fixture(self, contacts=(SELF, PEER), messages=(), groups=(), members=(), *, fts=True):
        with closing(sqlite3.connect(self.root / "profile_info.db")) as con, con:
            con.execute("CREATE TABLE profile_info_v6 ([1000] TEXT,[1002] INTEGER,[20002] TEXT,[20009] TEXT)")
            con.executemany("INSERT INTO profile_info_v6 VALUES (?,?,?,?)", contacts)
        with closing(sqlite3.connect(self.root / "group_info.db")) as con, con:
            con.execute("CREATE TABLE group_detail_info_ver1 ([60001] INTEGER,[60007] TEXT)")
            con.executemany("INSERT INTO group_detail_info_ver1 VALUES (?,?)", groups)
            con.execute("CREATE TABLE group_member3 ([64003] TEXT,[20002] TEXT,[60001] INTEGER,[1000] TEXT,[1001] TEXT,[1002] INTEGER)")
            con.executemany("INSERT INTO group_member3 VALUES (?,?,?,?,?,?)", members)
        common = "[40001] INTEGER PRIMARY KEY,[40050] INTEGER,[40020] TEXT,[40021] TEXT,[40003] INTEGER,[40027] INTEGER"
        with closing(sqlite3.connect(self.root / "nt_msg.db")) as con, con:
            for table in ("c2c_msg_table", "group_msg_table", "discuss_msg_table"):
                con.execute(f"CREATE TABLE {table} ({common},[40011] INTEGER,[40012] INTEGER,[40013] INTEGER,[40800] BLOB,[40900] BLOB,[40033] INTEGER,[40030] INTEGER)")
            for ident, sender_uid, peer_uid, sender_uin, kind, group_id in messages:
                table = {"private": "c2c", "group": "group", "discuss": "discuss"}[kind] + "_msg_table"
                con.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (ident,1700000000+ident,sender_uid,peer_uid,ident,group_id,0,1,0,b'',b'',sender_uin,0))
        if fts:
            for kind, table in (("private", "buddy_msg_fts"), ("group", "group_msg_fts"), ("discuss", "discuss_msg_fts")):
                with closing(sqlite3.connect(self.root / (table + ".db"))) as con, con:
                    con.execute(f"CREATE TABLE {table} ({common},[41701] TEXT,[41702] TEXT,[41703] INTEGER,[41704] INTEGER)")
                    for ident, sender_uid, peer_uid, sender_uin, msg_kind, group_id in messages:
                        if msg_kind == kind:
                            con.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?)", (ident,1700000000+ident,sender_uid,peer_uid,ident,group_id,"fixture","",0,1))

    def call(self, name="messages", **args):
        return adapter.call(name, {"contact": "u_peer", "include_media": False, "order": "asc", **args})


class ExactIdentityTests(IdentityFixture):
    def test_missing_stable_uin_or_uid_never_falls_back_to_fuzzy(self):
        self.fixture(contacts=[SELF, ("u_other_90001", 1900019, "u_missing", "90001")])
        for query in ("90001", "u_other", "u_missing"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.call(contact=query)

    def test_uin_and_uid_aliases_are_verified_exactly(self):
        self.fixture(messages=[(1, "u_peer", "u_peer", 20001, "private", 0)])
        for query in ("u_peer", "20001", "Fixture peer"):
            result = self.call(contact=query)
            self.assertEqual(result["chat"]["canonical_id"], "u_peer")
            self.assertEqual(result["chat"]["aliases"], ["u_peer", "20001"])
            self.assertTrue(result["chat"]["identity_verified"])
            self.assertEqual(result["messages"][0]["direction"], "from_contact")
            self.assertTrue(result["coverage"]["source_complete"])

    def test_exact_names_checked_beyond_discovery_candidate_cap(self):
        contacts = [SELF, ("u_first", 20000, "duplicate", "")]
        contacts += [(f"u_fuzzy_{i}", 30000+i, f"duplicate extra {i}", "") for i in range(399)]
        contacts += [("u_second", 50000, "duplicate", "")]
        self.fixture(contacts=contacts)
        discovery = adapter.call("resolve_contact", {"query": "duplicate", "limit": 100})
        self.assertFalse(discovery["complete"])
        self.assertTrue(discovery["truncated"])
        with self.assertRaises(ValueError):
            self.call(contact="duplicate")
        self.assertEqual(self.call(contact="u_second")["chat"]["uid"], "u_second")

    def test_exact_group_ambiguity_and_numeric_id_do_not_fuzzy_match(self):
        groups = [(101, "duplicate")] + [(1000+i, f"duplicate extra {i}") for i in range(399)] + [(202, "duplicate"), (303, "90001")]
        self.fixture(groups=groups)
        with self.assertRaises(ValueError):
            self.call(contact="duplicate", chat_type="group")
        with self.assertRaises(ValueError):
            self.call(contact="90001", chat_type="group")
        group = self.call(contact="202", chat_type="group")["chat"]
        self.assertEqual(group["canonical_id"], "202")
        self.assertEqual(group["aliases"], ["202"])
        self.assertTrue(group["identity_verified"])

    def test_missing_uid_fails_instead_of_false_empty(self):
        self.fixture(contacts=[SELF, (None, 222, "missing UID", "")],
                     messages=[(1, "u_actual", "u_actual", 222, "private", 0)])
        with self.assertRaisesRegex(ValueError, "missing UID"):
            self.call(contact="222")

    def test_conflicting_uid_uin_mapping_never_blesses_alias(self):
        self.fixture(contacts=[SELF, PEER, ("u_other", 20001, "different person", "")])
        for query in ("u_peer", "20001", "Fixture peer"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.call(contact=query)

    def test_chat_arguments_conflict_before_database_read(self):
        self.fixture()
        for args in ({"contact": "A", "group": "B"}, {"contact": "same", "group": "same"},
                     {"contact": "A", "chat": "B"}, {"contact": "A", "chat_type": "invalid"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.call(**args)
        with self.assertRaises(ValueError):
            adapter.call("messages", {"group": "123", "chat_type": "private"})


class SelfAndSenderTests(IdentityFixture):
    def test_missing_self_profile_cannot_adopt_similar_number(self):
        self.fixture(contacts=[("u_friend", 9100019, "Friend", "")],
                     messages=[(1, "u_friend", "u_friend", 9100019, "private", 0)])
        page = self.call(contact="u_friend")
        self.assertEqual(adapter._self_identity["status"], "unresolved")
        self.assertIsNone(adapter._self_identity["uid"])
        row = page["messages"][0]
        self.assertEqual(row["direction"], "unknown")
        self.assertNotEqual(row["sender"], "我")
        self.assertFalse(page["coverage"]["identity_complete"])
        self.assertFalse(page["coverage"]["source_complete"])
        self.assertIn("self_identity_unresolved", page["coverage"]["reasons"])

    def test_conflicting_self_environment_is_unknown(self):
        self.fixture(messages=[(1, "u_peer", "u_peer", 20001, "private", 0)])
        with patch.dict(os.environ, {"QQ_MCP_SELF_UID": "u_peer"}):
            page = self.call()
        self.assertEqual(adapter._self_identity["status"], "unresolved")
        self.assertEqual(page["messages"][0]["direction"], "unknown")

    def test_self_chat_only_contains_self_talker(self):
        self.fixture(contacts=[SELF, PEER, ("u_b", 20002, "B", "")], messages=[
            (1,"u_self","u_peer",10001,"private",0),
            (2,"u_self","u_b",10001,"private",0),
            (3,"u_peer","u_peer",20001,"private",0),
            (4,"u_self","u_self",10001,"private",0)])
        page = self.call(contact="u_self")
        self.assertEqual([r["msg_id"] for r in page["messages"]], ["4"])
        self.assertEqual(page["messages"][0]["peer_uid"], "u_self")
        normal = self.call(contact="u_peer")
        self.assertEqual([r["msg_id"] for r in normal["messages"]], ["1", "3"])

    def test_incoming_sender_peer_with_own_recipient_still_reads(self):
        self.fixture(messages=[(1,"u_peer","u_self",20001,"private",0),
                               (2,"u_peer","u_unrelated",20001,"private",0)])
        page = self.call()
        self.assertEqual([r["msg_id"] for r in page["messages"]], ["1"])

    def test_contradictory_sender_ids_have_warning_unknown_direction(self):
        self.fixture(messages=[(1,"u_peer","u_peer",10001,"private",0),
                               (2,"u_self","u_peer",20001,"private",0)])
        page = self.call()
        self.assertEqual(len(page["messages"]), 2)
        for row in page["messages"]:
            self.assertEqual(row["direction"], "unknown")
            self.assertEqual(row["identity_status"], "unresolved")
            self.assertTrue(any("sender_identity_conflict" in text for text in row["warnings"]))
        self.assertFalse(page["coverage"]["identity_complete"])
        stats = self.call("stats")
        self.assertEqual(stats["by_sender"], {"unknown": 2})
        self.assertEqual(stats["unknown_sender_count"], 2)
        self.assertEqual(stats["unresolved_sender_count"], 2)
        self.assertFalse(stats["coverage"]["identity_complete"])
        self.assertEqual(stats["senders"][0]["display_names"], ["unknown"])
        self.assertTrue(any("sender_identity_unresolved" in text for text in stats["warnings"]))

    def test_numeric_sender_uin_cannot_match_other_numeric_name(self):
        self.fixture(contacts=[SELF, ("u_a",111,"A",""),("u_b",222,"111","")],
                     groups=[(123,"Group")], members=[("A","A",123,"u_a","",111),("111","111",123,"u_b","",222)],
                     messages=[(1,"u_a","group",111,"group",123),(2,"u_b","group",222,"group",123)])
        by_uin = self.call(contact="123", chat_type="group", sender="111")
        self.assertEqual([r["msg_id"] for r in by_uin["messages"]], ["1"])
        by_name = self.call(contact="123", chat_type="group", sender_name="111")
        self.assertEqual([r["msg_id"] for r in by_name["messages"]], ["2"])
        by_uid = self.call(contact="123", chat_type="group", sender_uid="u_b")
        self.assertEqual([r["msg_id"] for r in by_uid["messages"]], ["2"])
        self.assertEqual(self.call(contact="123",chat_type="group",sender="uid:u_a")["messages"][0]["msg_id"], "1")
        with self.assertRaises(ValueError):
            self.call(contact="123",chat_type="group",sender="111",sender_name="111")

    def test_native_group_stats_keeps_same_name_ids_separate(self):
        self.fixture(groups=[(123,"Group")], members=[("Same","Same",123,"u_a","",111),("Same","Same",123,"u_b","",222)],
                     messages=[(1,"u_a","group",111,"group",123),(2,"u_b","group",222,"group",123)])
        stats = self.call("stats", contact="123", chat_type="group")
        self.assertEqual(stats["by_sender"], {"uid:u_a":1,"uid:u_b":1})
        self.assertEqual([item["display_names"] for item in stats["senders"]], [["Same"],["Same"]])


class CoverageTests(IdentityFixture):
    def test_missing_fts_page_end_is_not_source_complete(self):
        self.fixture(messages=[(1,"u_peer","u_peer",20001,"private",0)], fts=False)
        page = self.call()
        self.assertFalse(page["query"]["has_more"])
        self.assertTrue(page["coverage"]["pagination_complete"])
        self.assertFalse(page["coverage"]["source_complete"])
        self.assertEqual(page["coverage"]["reason"], "fts_unavailable")
        self.assertTrue(any("FTS unavailable" in text for text in page["warnings"]))
        self.assertEqual(page["query"]["coverage"], page["coverage"])
        stats = self.call("stats")
        self.assertFalse(stats["coverage"]["source_complete"])
        exported = self.call("export_messages", path=str(self.root / "export.jsonl"))
        self.assertFalse(exported["complete"])
        self.assertFalse(exported["truncated"])
        self.assertEqual(exported["coverage"]["reason"], "fts_unavailable")

    def test_available_sources_with_more_pages_are_not_pagination_complete(self):
        self.fixture(messages=[(1,"u_peer","u_peer",20001,"private",0),(2,"u_peer","u_peer",20001,"private",0)])
        first = self.call(limit=1)
        self.assertTrue(first["coverage"]["source_complete"])
        self.assertFalse(first["coverage"]["pagination_complete"])
        last = self.call(limit=1,cursor=first["query"]["next_cursor"])
        self.assertTrue(last["coverage"]["pagination_complete"])
        self.assertTrue(last["coverage"]["source_complete"])

    def test_sender_namespace_is_bound_to_cursor(self):
        self.fixture(messages=[(1,"u_peer","u_peer",20001,"private",0),(2,"u_peer","u_peer",20001,"private",0)])
        first = self.call(limit=1,sender_uid="u_peer")
        with self.assertRaises(ValueError):
            self.call(limit=1,sender_name="Fixture peer",cursor=first["query"]["next_cursor"])


if __name__ == "__main__":
    unittest.main()
