"""Adversarial read-scope cases using invented records and actual routing."""
import json
import unittest

from unified_mcp import analysis_tools
from unified_mcp.server import Gateway
from unified_mcp.timeline import decoded_result, merged_timeline


def row(source="wechat", chat="wxid_intended", timestamp=100, **extra):
    if source == "wechat":
        return {"id": {"talker": chat, "local_id": 1, "server_id_str": "7"},
                "create_time": timestamp, "kind": "text", "text": "intended", **extra}
    return {"chat_id": chat, "msg_id": "7", "timestamp": timestamp, "kind": "text",
            "sender_uid": "u_member", "text": "intended", **extra}


def page(rows, *, more=False, **extra):
    return {"messages": rows, "query": {"has_more": more}, **extra}


def qq_chat(uid="u_intended", uin="12345"):
    return {"uid": uid, "uin": uin, "canonical_id": uid,
            "aliases": [uid, uin], "identity_verified": True, "chat_type": "private"}


class ReadContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_chat_body_never_leaks_in_partial_pages(self):
        gateway = Gateway()
        async def fetch(*_):
            return page([row(chat="wxid_foreign", text="FOREIGN_BODY_SENTINEL")])
        gateway.fetch = fetch
        try:
            result = await gateway.call("unified_timeline", {"wechat_chat": "wxid_intended", "include_media": False})
            encoded = result.model_dump_json()
            self.assertTrue(result.isError)
            self.assertNotIn("FOREIGN_BODY_SENTINEL", encoded)
            self.assertNotIn("wxid_foreign", encoded)
            payload = json.loads(result.content[0].text)
            self.assertEqual(payload["available_source_pages"], {})
        finally:
            await gateway.close()

    async def test_native_wechat_scope_checked_even_with_media_disabled(self):
        from unittest.mock import AsyncMock
        from unified_mcp.server import text_result
        gateway = Gateway()
        gateway.wechat.call = AsyncMock(return_value=text_result(page([row(chat="wxid_foreign", text="FOREIGN_BODY")])))
        try:
            result = await gateway.call("chat_timeline", {"talker": "wxid_intended", "include_media_paths": False})
            self.assertTrue(result.isError)
            self.assertNotIn("FOREIGN_BODY", result.model_dump_json())
        finally:
            await gateway.close()

    async def test_scoped_native_backend_error_never_echoes_unverified_body(self):
        from unittest.mock import AsyncMock
        from unified_mcp.server import text_result
        gateway = Gateway()
        gateway.wechat.call = AsyncMock(return_value=text_result(page([row(chat="wxid_foreign", text="FOREIGN_BODY")]), error=True))
        try:
            result = await gateway.call("chat_timeline", {"talker": "wxid_intended"})
            self.assertTrue(result.isError)
            self.assertNotIn("FOREIGN_BODY", result.model_dump_json())
        finally:
            await gateway.close()

    async def test_unvalidated_envelope_fields_never_enter_partial_result(self):
        async def fetch(source, *_):
            if source == "qq":
                raise RuntimeError("reader unavailable")
            return page([row()], unvalidated_extra={"text": "FOREIGN_BODY"})
        result = await merged_timeline({"wechat_chat": "wxid_intended", "qq_chat": "u_intended"}, fetch)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["available_source_pages"]["wechat"]["messages"]), 1)
        self.assertNotIn("FOREIGN_BODY", json.dumps(result))

    async def test_same_numeric_id_does_not_cross_private_and_group_types(self):
        async def fetch(*_):
            return page([row("qq", "12345", chat_type="group")],
                        chat={"group_id": "12345", "canonical_id": "12345", "aliases": ["12345"],
                              "identity_verified": True, "chat_type": "group"})
        result = await merged_timeline({"qq_chat": "12345", "qq_chat_type": "private"}, fetch)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["available_source_pages"])
        result = await analysis_tools.message({"source": "qq", "chat_id": "12345", "chat_type": "private", "message_id": "7"}, fetch)
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("message", result)

    async def test_media_requery_collision_cannot_replace_confirmed_message(self):
        async def fetch(_, __, params):
            good = row()
            if params["include_media_paths"]:
                return page([row(text="WRONG_BODY", images=[{"path": "wrong.png"}]), good])
            return page([good])
        result = await analysis_tools.message({"source": "wechat", "chat_id": "wxid_intended", "message_id": "7"}, fetch)
        self.assertEqual(result["message"]["text"], "intended")
        self.assertNotIn("images", result["message"]["original"])
        self.assertTrue(any("ambiguous" in warning for warning in result["warnings"]))

    async def test_media_requery_source_text_change_is_not_silently_adopted(self):
        async def fetch(_, __, params):
            return page([row(text="changed" if params["include_media_paths"] else "intended")])
        result = await analysis_tools.message({"source": "wechat", "chat_id": "wxid_intended", "message_id": "7"}, fetch)
        self.assertEqual(result["message"]["text"], "intended")
        self.assertTrue(any("changed" in warning for warning in result["warnings"]))

    async def test_all_explicit_chat_fields_must_agree(self):
        async def fetch(*_):
            return page([row(chat="wxid_other", talker="wxid_intended")])
        result = await merged_timeline({"wechat_chat": "wxid_intended"}, fetch)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["available_source_pages"])

    async def test_valid_source_stays_available_when_other_identity_fails(self):
        async def fetch(source, *_):
            return page([row()]) if source == "wechat" else page([row("qq", "u_other", text="FOREIGN")])
        result = await merged_timeline({"wechat_chat": "wxid_intended", "qq_chat": "u_intended"}, fetch)
        self.assertEqual(set(result["available_source_pages"]), {"wechat"})
        self.assertNotIn("FOREIGN", json.dumps(result))

    async def test_dictionary_error_cannot_become_successful_empty_read(self):
        for error in ({"error": "failed"}, {"errors": ["failed"]}, {"status": "partial"}):
            async def fetch(*_):
                return page([], **error)
            result = await merged_timeline({"qq_chat": "u_intended"}, fetch)
            self.assertEqual(result["status"], "partial")
            self.assertFalse(result["available_source_pages"])

    async def test_verified_uin_alias_returns_canonical_uid_and_record_id(self):
        async def fetch(*_):
            return page([row("qq", "u_intended")], chat=qq_chat())
        result = await merged_timeline({"qq_chat": "12345"}, fetch)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["messages"][0]["chat_id"], "u_intended")
        located = await analysis_tools.message({"source": "qq", "chat_id": "12345", "message_id": "7", "include_media": False}, fetch)
        self.assertEqual(located["status"], "ok")
        self.assertEqual(located["message"]["record_id"], "u_intended:7")

    async def test_nickname_is_never_trusted_as_an_identity_alias(self):
        chat = qq_chat()
        chat["aliases"].append("same-name")
        async def fetch(*_):
            return page([row("qq", "u_intended")], chat=chat)
        result = await merged_timeline({"qq_chat": "same-name"}, fetch)
        self.assertEqual(result["status"], "partial")

    async def test_alias_cannot_silently_change_between_pages(self):
        async def first(*_):
            return page([row("qq", "u_intended")], chat=qq_chat(), more=True)
        args = {"qq_chat": "12345", "limit": 1}
        first_page = await merged_timeline(args, first)
        async def changed(*_):
            return page([row("qq", "u_other")], chat=qq_chat(uid="u_other"))
        result = await merged_timeline({**args, "cursor": first_page["next_cursor"]}, changed)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["available_source_pages"])

    async def test_degraded_source_cannot_prove_absence_or_group_completeness(self):
        warning = "QQ FTS unavailable; main payloads only"
        async def fetch(*_):
            return page([], warnings=[warning], coverage={"source_complete": False})
        for function, options in ((analysis_tools.message, {"message_id": "missing"}),
                                  (analysis_tools.group_stats, {"chat_type": "group"})):
            result = await function({"source": "qq", "chat_id": "12345", "include_media": False, **options}, fetch)
            self.assertEqual(result["status"], "partial")
            self.assertFalse(result["coverage"]["complete"])
            self.assertTrue(result["coverage"]["pagination_complete"])
            self.assertIn(warning, result["warnings"])

    async def test_optional_media_warning_does_not_erase_complete_text_read(self):
        async def fetch(*_):
            return page([row()], warnings=["OCR unavailable"])
        result = await analysis_tools.message({"source": "wechat", "chat_id": "wxid_intended", "message_id": "7", "include_media": False}, fetch)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["coverage"]["complete"])
        self.assertIn("OCR unavailable", result["warnings"])

    async def test_conflicting_sender_cannot_be_counted_as_a_known_group_member(self):
        async def fetch(*_):
            return page([row("qq", "12345", sender_uid="u_member", sender_uin="10001", direction="unknown",
                             identity_status="unresolved", warnings=["sender_identity_conflict: synthetic contradiction"])])
        result = await analysis_tools.group_stats({"source": "qq", "chat_id": "12345"}, fetch)
        self.assertEqual(result["summary"]["unknown_sender_count"], 1)
        self.assertEqual(result["summary"]["unresolved_identity_count"], 1)
        self.assertFalse(result["coverage"]["identity_complete"])
        self.assertIsNone(result["summary"]["senders"][0]["sender_id"])

    async def test_page_is_validated_before_emitting_early_matching_record(self):
        async def fetch(*_):
            return page([row(), row(chat="wxid_foreign", text="FOREIGN")])
        result = await analysis_tools.message({"source": "wechat", "chat_id": "wxid_intended", "message_id": "7", "include_media": False}, fetch)
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("candidates", result)
        self.assertNotIn("FOREIGN", json.dumps(result))

    async def test_two_platform_sender_must_be_separately_bound(self):
        calls = []
        async def fetch(source, _, args):
            calls.append((source, args["sender"]))
            return page([])
        args = {"wechat_chat": "wxid_intended", "qq_chat": "u_intended"}
        with self.assertRaisesRegex(ValueError, "separately"):
            await merged_timeline({**args, "sender": "one-name"}, fetch)
        self.assertFalse(calls)
        result = await merged_timeline({**args, "wechat_sender": "wxid_member", "qq_sender": "uid:u_member"}, fetch)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, [("wechat", "wxid_member"), ("qq", "uid:u_member")])

    async def test_exported_collision_id_selects_the_correct_record_and_context(self):
        from unified_mcp.record_identity import extended_record_id
        from unified_mcp.timeline import normal_message
        first = row(text="first version")
        second = row(text="second version")
        base = normal_message("wechat", first, "wxid_intended")["record_id"]
        selected = extended_record_id(base, second)
        async def fetch(_, __, params):
            records = [first, second] if params["order"] == "asc" else [second, first]
            return page(records)
        args = {"source": "wechat", "chat_id": "wxid_intended", "record_id": selected, "include_media": False}
        result = await analysis_tools.message(args, fetch)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["message"]["text"], "second version")
        self.assertEqual(result["message"]["record_id"], selected)
        window = await analysis_tools.context({**args, "before_count": 1, "after_count": 0}, fetch)
        self.assertEqual(window["before"][0]["text"], "first version")
        self.assertEqual(window["target"]["record_id"], selected)

    async def test_exact_record_id_accepts_verified_qq_account_alias(self):
        async def fetch(*_):
            return page([row("qq", "u_intended")], chat=qq_chat())
        result = await analysis_tools.message({"source": "qq", "chat_id": "12345", "record_id": "u_intended:7", "include_media": False}, fetch)
        self.assertEqual(result["status"], "ok")
        with self.assertRaisesRegex(ValueError, "different chat"):
            await analysis_tools.message({"source": "qq", "chat_id": "12345", "record_id": "u_other:7", "include_media": False}, fetch)


if __name__ == "__main__":
    unittest.main()
