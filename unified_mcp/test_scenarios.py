"""User-intent acceptance through real Gateway.call with invented reader data.

Run: python -m unittest unified_mcp.test_scenarios -v
These tests never connect to WeChat, QQ, a private account or an ASR model.
"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from unified_mcp.server import Gateway
from unified_mcp.scenario_support import (
    DAY, QQ_GROUP, QQ_PERSON, WX_GROUP, WX_OTHER, WX_PERSON,
    SyntheticOCR, SyntheticReaders, SyntheticVoice, epoch,
)


def payload(result):
    return json.loads(next(block.text for block in result.content if block.type == "text"))


class UserScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wxqq-synthetic-")
        self.fixture = SyntheticReaders(Path(self.temporary.name))
        self.voice = SyntheticVoice()
        self.ocr = SyntheticOCR()
        self.patches = [
            patch("unified_mcp.server.WeChatBackend", return_value=self.fixture.wechat()),
            patch("unified_mcp.server.VoiceService", return_value=self.voice),
            # The fixture already supplies decoded local images. Never search any
            # real account's media directories to enrich an invented record.
            patch("unified_mcp.wechat_media.enrich_wechat_media", side_effect=lambda value, *_: value),
        ]
        for item in self.patches:
            item.start()
        self.gateway = Gateway()
        self.gateway.qq = self.fixture.qq()
        self.gateway.image_text = self.ocr

    async def asyncTearDown(self):
        await self.gateway.close()
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    async def read(self, name="unified_timeline", **args):
        result = await self.gateway.call(name, args)
        return result, payload(result)

    async def timeline(self, **args):
        options = {"wechat_chat": WX_PERSON, "after": DAY, "before": DAY,
                   "include_media": False, "include_image_text": False, **args}
        return await self.read(**options)

    async def exact(self, **args):
        return await self.read("unified_message", source="wechat", chat_id=WX_PERSON,
                               date=DAY, include_media=False, include_image_text=False, **args)

    async def collect(self, **args):
        records, pages = [], []
        for _ in range(100):
            response, page = await self.read(**args)
            self.assertFalse(response.isError, page)
            pages.append(page)
            records.extend(page["messages"])
            if not page["has_more"]:
                self.assertIsNone(page["next_cursor"])
                return records, pages
            self.assertTrue(page["next_cursor"])
            args["cursor"] = page["next_cursor"]
        self.fail("The user asked for all history, but pagination never reached an endpoint")

    async def test_s01_same_nickname_requires_identity_selection(self):
        _, result = await self.read("unified_resolve_chat", query="Alex")
        self.assertEqual(len(result["wechat"]["candidates"]), 2)
        self.assertEqual(len(result["qq"]["candidates"]), 1)
        self.assertNotIn("same_person", result)
        self.assertTrue(all(name.startswith("resolve_") for _, name, _ in self.fixture.calls))

    async def test_s02_one_person_never_leaks_namesake_messages(self):
        response, page = await self.timeline()
        self.assertFalse(response.isError)
        self.assertEqual({r["chat_id"] for r in page["messages"]}, {WX_PERSON})
        self.assertNotIn("different person secret", json.dumps(page))
        self.assertTrue(all(a.get("talker") == WX_PERSON for _, _, a in self.fixture.calls))

    async def test_s03_day_includes_midnight_and_last_second_not_next_day(self):
        _, page = await self.timeline()
        self.assertEqual(len(page["messages"]), 10)
        self.assertEqual({r["message_id"] for r in page["messages"]}, set(map(str, range(2, 11))))
        self.assertTrue(all(epoch(DAY) <= r["timestamp"] < epoch("2026-01-03") for r in page["messages"]))

    async def test_s04_explicit_utc_window_selects_same_local_day(self):
        _, page = await self.timeline(after="2026-01-01T16:00:00+00:00", before="2026-01-02T16:00:00+00:00")
        self.assertEqual(len(page["messages"]), 10)
        self.assertEqual(self.fixture.calls[0][2]["after"], str(epoch(DAY)))

    async def test_s05_all_history_requires_multiple_pages_and_terminal_proof(self):
        rows, pages = await self.collect(wechat_chat=WX_PERSON, qq_chat=QQ_PERSON,
                                        order="asc", limit=2, include_media=False, include_image_text=False)
        self.assertEqual(len(rows), 17)
        self.assertGreater(len(pages), 1)
        self.assertEqual(len({(r["source"], r["record_id"]) for r in rows}), 17)
        self.assertEqual([r["timestamp"] for r in rows], sorted(r["timestamp"] for r in rows))
        self.assertIn("Local", pages[-1]["coverage"])

    async def test_s06_newest_first_also_reaches_all_records(self):
        rows, _ = await self.collect(wechat_chat=WX_PERSON, qq_chat=QQ_PERSON,
                                     order="desc", limit=2, include_media=False, include_image_text=False)
        self.assertEqual(len(rows), 17)
        self.assertEqual([r["timestamp"] for r in rows], sorted((r["timestamp"] for r in rows), reverse=True))

    async def test_s07_same_second_and_shared_server_id_remain_distinct(self):
        rows, _ = await self.collect(wechat_chat=WX_PERSON, after=DAY, before=DAY,
                                     order="asc", limit=1, include_media=False, include_image_text=False)
        self.assertEqual([r["original"]["id"]["local_id"] for r in rows if r["timestamp"] == epoch(DAY + "T09:00:00+08:00")], [3, 4, 40])

    async def test_s08_cross_platform_search_preserves_sources(self):
        result, page = await self.read("unified_search", wechat_chat=WX_PERSON, qq_chat=QQ_PERSON,
                                       keyword="deadline", after=DAY, before=DAY, include_media=False)
        self.assertFalse(result.isError)
        self.assertEqual({r["source"] for r in page["messages"]}, {"wechat", "qq"})
        self.assertTrue(all("deadline" in r["text"] for r in page["messages"]))

    async def test_s09_blank_search_is_rejected_before_any_read(self):
        result, _ = await self.read("unified_search", wechat_chat=WX_PERSON, keyword="  ")
        self.assertTrue(result.isError)
        self.assertEqual(self.fixture.calls, [])

    async def test_s10_existing_chat_empty_day_is_not_reader_failure(self):
        result, page = await self.timeline(after="2020-01-01", before="2020-01-01")
        self.assertFalse(result.isError)
        self.assertEqual(page["messages"], [])
        self.assertFalse(page["has_more"])

    async def test_s11_nonexistent_chat_is_not_success_with_empty_messages(self):
        result, page = await self.timeline(wechat_chat="wxid_synthetic_missing")
        self.assertTrue(result.isError)
        self.assertEqual(page["status"], "partial")
        self.assertIn("wechat", page["errors"])

    async def test_s12_one_platform_down_does_not_claim_complete_merge(self):
        self.fixture.fail_source = "qq"
        result, page = await self.timeline(qq_chat=QQ_PERSON)
        self.assertTrue(result.isError)
        self.assertEqual(page["status"], "partial")
        self.assertIsNone(page["next_cursor"])
        self.assertIn("wechat", page["available_source_pages"])

    async def test_s13_explicit_single_source_still_works_when_other_is_down(self):
        self.fixture.fail_source = "qq"
        result, page = await self.timeline()
        self.assertFalse(result.isError)
        self.assertEqual(len(page["messages"]), 10)
        self.assertTrue(all(source == "wechat" for source, _, _ in self.fixture.calls))

    async def test_s14_cursor_cannot_be_reused_for_a_different_person(self):
        _, page = await self.timeline(limit=1)
        result, _ = await self.timeline(wechat_chat=WX_OTHER, limit=1, cursor=page["next_cursor"])
        self.assertTrue(result.isError)
        self.assertEqual(len(self.fixture.calls), 1)

    async def test_s15_unknown_sender_stays_unknown(self):
        _, page = await self.timeline()
        row = next(r for r in page["messages"] if r["message_id"] == "5")
        self.assertIsNone(row["sender_id"])
        self.assertEqual(row["direction"], "unknown")

    async def test_s16_qq_group_read_preserves_actual_member_ids(self):
        result, page = await self.read(qq_chat=QQ_GROUP, qq_chat_type="group", after=DAY, before=DAY, include_media=False)
        self.assertFalse(result.isError)
        self.assertEqual({r["sender_id"] for r in page["messages"]}, {"qa", "qb", None})
        self.assertEqual(self.fixture.calls[0][2]["chat_type"], "group")

    async def test_s17_read_one_cached_voice_with_automated_label(self):
        result, page = await self.read("messages", talker=WX_PERSON, after=DAY, before=DAY,
                                       server_id_str="6", include_image_text=False)
        self.assertFalse(result.isError)
        self.assertEqual(len(page["messages"]), 1)
        voice = page["messages"][0]["voice"]["transcript"]
        self.assertEqual(voice["text"], "Bring the blue folder tomorrow")
        self.assertTrue(voice["automatic"])
        self.assertFalse(voice["human_verified"])

    async def test_s18_missing_voice_file_is_not_an_invented_transcript(self):
        _, page = await self.read("messages", talker=WX_PERSON, server_id_str="9", include_image_text=False)
        voice = page["messages"][0]["voice"]
        self.assertEqual(voice["status"], "unavailable")
        self.assertNotIn("transcript", voice)

    async def test_s19_picture_remains_viewable_when_ocr_is_unavailable(self):
        self.ocr.status = "unavailable"
        result, page = await self.read("unified_read_image", path=str(self.fixture.image))
        self.assertEqual(page["ocr"]["status"], "unavailable")
        self.assertEqual(len([b for b in result.content if b.type == "image"]), 1)
        self.assertFalse(result.isError)

    async def test_s20_animated_sticker_preview_declares_first_frame(self):
        result, page = await self.read("unified_read_image", path=str(self.fixture.sticker))
        self.assertFalse(result.isError)
        self.assertEqual(len([b for b in result.content if b.type == "image"]), 1)
        self.assertEqual(page["path"], str(self.fixture.sticker))
        self.assertIn("first", json.dumps(page).lower())

    async def test_s21_broken_jpeg_does_not_look_like_a_readable_image(self):
        result, page = await self.read("unified_read_image", path=str(self.fixture.broken))
        self.assertTrue(result.isError)
        self.assertEqual(len([b for b in result.content if b.type == "image"]), 0)

    async def test_s22_text_only_read_does_not_load_asr_or_ocr(self):
        result, _ = await self.timeline()
        self.assertFalse(result.isError)
        self.assertEqual(self.voice.calls, 0)
        self.assertEqual(self.ocr.calls, [])

    async def test_s23_cancel_read_then_use_gateway_again(self):
        self.fixture.block_wechat = True
        task = asyncio.create_task(self.timeline())
        await asyncio.wait_for(self.fixture.wait_started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.fixture.block_wechat = False
        result, page = await self.timeline()
        self.assertFalse(result.isError)
        self.assertEqual(len(page["messages"]), 10)

    async def test_s24_emoji_and_chinese_survive_tool_json(self):
        _, page = await self.timeline()
        self.assertEqual(next(r["text"] for r in page["messages"] if r["message_id"] == "10"), "end boundary 👩🏽‍💻 中文")

    async def test_s25_tool_catalog_advertises_specific_read_context_stats(self):
        names = {t.name for t in self.gateway.tools()}
        self.assertTrue({"unified_message", "unified_context", "unified_group_stats"}.issubset(names))
        self.assertFalse({"send_message", "send", "transfer_money"} & names)

    async def test_s26_single_message_lookup_returns_exact_record(self):
        result, data = await self.exact(message_id="3")
        self.assertFalse(result.isError)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["message"]["text"], "deadline Friday")
        self.assertEqual(data["message"]["chat_id"], WX_PERSON)

    async def test_s27_shared_id_requires_disambiguation_not_arbitrary_choice(self):
        _, data = await self.exact(message_id="4")
        self.assertEqual(data["status"], "ambiguous")
        self.assertEqual(len(data["candidates"]), 2)
        self.assertNotIn("message", data)

    async def test_s28_local_record_identity_disambiguates_shared_id(self):
        _, page = await self.timeline()
        row = next(r for r in page["messages"] if r["original"]["id"]["local_id"] == 40)
        result, data = await self.exact(record_id=row["record_id"])
        self.assertFalse(result.isError)
        self.assertEqual(data["message"]["original"]["id"]["local_id"], 40)

    async def test_s29_missing_message_needs_complete_search_before_not_found(self):
        _, data = await self.exact(message_id="99999")
        self.assertEqual(data["status"], "not_found")
        self.assertTrue(data["coverage"]["complete"])
        self.assertNotIn("message", data)

    async def test_s30_scan_cap_must_not_report_missing_as_certain(self):
        _, data = await self.exact(message_id="99999", max_scan=2)
        self.assertEqual(data["status"], "partial")
        self.assertFalse(data["coverage"]["complete"])
        self.assertTrue(data["coverage"]["limited"])

    async def test_s31_context_includes_same_second_neighbors_in_order(self):
        result, data = await self.read("unified_context", source="wechat", chat_id=WX_PERSON,
                                       message_id="3", date=DAY, before_count=1, after_count=3,
                                       include_media=False, include_image_text=False)
        self.assertFalse(result.isError, data)
        self.assertEqual(data["target"]["message_id"], "3")
        self.assertEqual([r["original"]["id"]["local_id"] for r in data["before"]], [2])
        self.assertEqual([r["original"]["id"]["local_id"] for r in data["after"]], [4, 40, 5])
        self.assertTrue(all(r["chat_id"] == WX_PERSON for r in data["before"] + data["after"]))

    async def test_s32_group_stats_count_identities_not_display_names(self):
        result, data = await self.read("unified_group_stats", source="wechat", chat_id=WX_GROUP, date=DAY)
        self.assertFalse(result.isError, data)
        self.assertEqual(data["summary"]["total_messages"], 5)
        self.assertEqual(data["summary"]["unknown_sender_count"], 1)
        senders = {r["sender_id"]: r["count"] for r in data["summary"]["senders"] if r["sender_id"]}
        self.assertEqual(senders, {"member-a": 2, "member-b": 1, "self": 1})
        self.assertEqual(data["summary"]["by_kind"], {"text": 4, "system": 1})
        self.assertTrue(data["coverage"]["complete"])
        self.assertNotIn("sentiment", data["summary"])

    async def test_s33_limited_group_stats_are_partial_observations(self):
        _, data = await self.read("unified_group_stats", source="qq", chat_id=QQ_GROUP, date=DAY, max_messages=2)
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["summary"]["total_messages"], 2)
        self.assertFalse(data["coverage"]["complete"])
        self.assertTrue(data["coverage"]["limited"])

    async def test_s34_day_shorthand_has_the_same_clear_boundaries(self):
        result, page = await self.read(wechat_chat=WX_PERSON, date=DAY, include_media=False)
        self.assertFalse(result.isError, page)
        self.assertEqual(len(page["messages"]), 10)

    async def test_s35_conflicting_date_and_range_are_rejected(self):
        result, _ = await self.read(wechat_chat=WX_PERSON, date=DAY, after="2025-01-01")
        self.assertTrue(result.isError)
        self.assertEqual(self.fixture.calls, [])

    async def test_s36_filter_one_sender_in_group(self):
        result, page = await self.read(wechat_chat=WX_GROUP, date=DAY, sender="member-a", include_media=False)
        self.assertFalse(result.isError, page)
        self.assertEqual([r["sender_id"] for r in page["messages"]], ["member-a", "member-a"])

    async def test_s37_find_only_voice_messages(self):
        result, page = await self.read(wechat_chat=WX_PERSON, date=DAY, kind_name="voice", include_media=False)
        self.assertFalse(result.isError, page)
        self.assertEqual({r["message_id"] for r in page["messages"]}, {"6", "9"})

    async def test_s38_cursor_cannot_change_sender_mid_query(self):
        _, page = await self.read(wechat_chat=WX_GROUP, date=DAY, sender="member-a", limit=1, include_media=False)
        result, _ = await self.read(wechat_chat=WX_GROUP, date=DAY, sender="member-b", limit=1,
                                    include_media=False, cursor=page["next_cursor"])
        self.assertTrue(result.isError)
        self.assertEqual(len(self.fixture.calls), 1)

    async def test_s40_empty_nonterminal_backend_page_cannot_loop_forever(self):
        async def stuck(*_):
            from unified_mcp.server import text_result
            return text_result({"messages": [], "query": {"has_more": True}})
        self.gateway.wechat.call = stuck
        result, page = await self.timeline()
        self.assertTrue(result.isError, page)
        self.assertEqual(page["status"], "partial")
        self.assertIsNone(page["next_cursor"])

    async def test_s41_record_id_optimization_must_respect_requested_day(self):
        _, page = await self.timeline()
        target = next(r for r in page["messages"] if r["message_id"] == "3")
        _, data = await self.read("unified_message", source="wechat", chat_id=WX_PERSON,
                                  record_id=target["record_id"], date="2026-01-01", include_media=False)
        self.assertEqual(data["status"], "not_found")
        self.assertNotIn("message", data)

    async def test_s42_record_id_cannot_select_a_different_chat(self):
        _, page = await self.timeline()
        target = next(r for r in page["messages"] if r["message_id"] == "3")
        response, data = await self.read("unified_message", source="wechat", chat_id=WX_OTHER,
                                         record_id=target["record_id"], date=DAY, include_media=False)
        self.assertNotEqual(data.get("status"), "ok")
        self.assertNotIn("message", data)
        # Either a scope error or a proved not_found is legitimate; an unrelated
        # chat must never silently replace the user's explicitly selected chat.
        self.assertTrue(response.isError or data.get("status") == "not_found")

    async def test_s43_named_image_message_can_be_followed_to_actual_preview(self):
        response, data = await self.read("unified_message", source="wechat", chat_id=WX_PERSON,
                                         message_id="7", date=DAY, include_media=True, include_image_text=False)
        self.assertFalse(response.isError, data)
        self.assertEqual(data["message"]["kind"], "image")
        path = data["message"]["original"]["images"][0]["path"]
        image_result, _ = await self.read("unified_read_image", path=path)
        self.assertFalse(image_result.isError)
        self.assertEqual(sum(b.type == "image" for b in image_result.content), 1)

    async def test_s44_named_sticker_message_can_be_followed_to_gif_preview(self):
        response, data = await self.read("unified_message", source="wechat", chat_id=WX_PERSON,
                                         message_id="8", date=DAY, include_media=True, include_image_text=False)
        self.assertFalse(response.isError, data)
        self.assertEqual(data["message"]["kind"], "sticker")
        path = data["message"]["original"]["images"][0]["path"]
        image_result, image_data = await self.read("unified_read_image", path=path)
        self.assertFalse(image_result.isError)
        self.assertEqual(sum(b.type == "image" for b in image_result.content), 1)
        self.assertIn("first", json.dumps(image_data).lower())

    async def test_s45_missing_terminal_marker_does_not_prove_complete_history(self):
        original = self.fixture.native
        def without_marker(source, name, args):
            page = original(source, name, args)
            page["query"].pop("has_more", None)
            return page
        self.fixture.native = without_marker
        _, data = await self.read("unified_group_stats", source="wechat", chat_id=WX_GROUP, date=DAY)
        self.assertEqual(data["status"], "partial")
        self.assertFalse(data["coverage"]["complete"])

    async def test_s46_empty_sender_identifier_is_counted_as_unknown(self):
        self.fixture.rows["wechat", WX_GROUP][1]["sender_wxid"] = ""
        _, data = await self.read("unified_group_stats", source="wechat", chat_id=WX_GROUP, date=DAY)
        self.assertEqual(data["summary"]["unknown_sender_count"], 2)
        self.assertFalse(any(r["sender_id"] == "" for r in data["summary"]["senders"]))

    async def test_s47_backend_foreign_chat_record_is_not_relabeled_as_this_group(self):
        foreign = dict(self.fixture.rows["wechat", WX_OTHER][0], talker=WX_OTHER)
        self.fixture.rows["wechat", WX_GROUP].append(foreign)
        response, data = await self.read("unified_group_stats", source="wechat", chat_id=WX_GROUP, date=DAY)
        self.assertTrue(response.isError)
        self.assertEqual(data["status"], "partial")
        self.assertNotIn("different person secret", json.dumps(data))

    async def test_s48_out_of_order_reader_cannot_claim_reliable_context(self):
        original = self.fixture.native
        def wrong_order(source, name, args):
            page = original(source, name, args)
            page["messages"].reverse()
            return page
        self.fixture.native = wrong_order
        response, data = await self.read("unified_context", source="wechat", chat_id=WX_PERSON,
                                         message_id="3", date=DAY, include_media=False)
        self.assertTrue(response.isError)
        self.assertEqual(data["status"], "partial")

    async def test_s49_exact_qq_group_message_never_uses_private_chat_table(self):
        response, data = await self.read("unified_message", source="qq", chat_id=QQ_GROUP,
                                         chat_type="group", message_id="301", date=DAY, include_media=False)
        self.assertFalse(response.isError, data)
        self.assertEqual(data["message"]["text"], "group agenda")
        self.assertTrue(all(args["chat_type"] == "group" for _, _, args in self.fixture.calls))

    async def test_s50_found_id_in_partial_scan_still_requires_uniqueness_proof(self):
        _, data = await self.exact(message_id="3", max_scan=2)
        self.assertEqual(data["status"], "partial")
        self.assertNotIn("message", data)
        self.assertEqual(len(data["candidates"]), 1)


class CliEncodingScenarioTests(unittest.TestCase):
    def test_s39_windows_gbk_pipe_still_emits_utf8_json(self):
        env = {**os.environ, "PYTHONIOENCODING": "gbk", "UNIFIED_WECHAT_COMMAND": "synthetic-never-start-reader"}
        result = subprocess.run([sys.executable, "-m", "unified_mcp.server", "--call", "synthetic_👩🏽‍💻_不存在"],
                                cwd=Path(__file__).resolve().parent.parent, env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(data["tool"], "synthetic_👩🏽‍💻_不存在")
        self.assertNotIn(b"UnicodeEncodeError", result.stderr)


if __name__ == "__main__":
    unittest.main()
