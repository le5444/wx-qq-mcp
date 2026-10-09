import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from unified_mcp.media_inventory import export


def result(value):
    return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text=json.dumps(value))])


class MediaInventoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "out"
        self.options = SimpleNamespace(output=self.output, kinds=["voice"], talker="target",
                                       before=100, page_size=10)
        self.raw = {"talker": "target", "local_id": 1, "server_id_str": "15",
                    "kind_name": "voice", "create_time": 20, "create_time_human": "time",
                    "message_content": "<voice/>"}
        self.agent = {"id": {"local_id": 1, "server_id_str": "15"}, "kind": "voice", "create_time": 20}

    async def run_export(self, values):
        backend = SimpleNamespace(call=AsyncMock(side_effect=[result(v) for v in values]), close=AsyncMock())
        with patch("unified_mcp.media_inventory.WeChatBackend", return_value=backend):
            await export(self.options)
        return backend

    def report(self):
        return json.loads((self.output / "manifest.json").read_text(encoding="utf8"))

    async def test_verified_terminal_page_keeps_full_metadata(self):
        backend = await self.run_export([[self.raw], {"query": {"has_more": False}, "messages": [self.agent]}])
        self.assertTrue(self.report()["complete"])
        self.assertEqual(json.loads((self.output / "voice.jsonl").read_text(encoding="utf8")), self.raw)
        self.assertEqual(backend.call.call_args_list[0].args[1]["before"], "100")
        self.assertFalse(backend.call.call_args_list[0].args[1]["include_media_paths"])
        backend.close.assert_awaited_once()

    async def test_missing_terminal_evidence_leaves_incomplete_report(self):
        with self.assertRaisesRegex(RuntimeError, "Missing pagination"):
            await self.run_export([[self.raw], {"messages": [self.agent]}])
        self.assertFalse(self.report()["complete"])
        self.assertEqual((self.output / "voice.jsonl").read_text(encoding="utf8"), "")

    async def test_different_full_and_agent_records_fail_before_writing(self):
        wrong = {**self.agent, "create_time": 21}
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            await self.run_export([[self.raw], {"query": {"has_more": False}, "messages": [wrong]}])
        self.assertEqual((self.output / "voice.jsonl").read_text(encoding="utf8"), "")

    async def test_following_cursor_is_used_until_explicit_terminal_page(self):
        first = {"query": {"has_more": True, "next_offset": 1}, "messages": [self.agent]}
        backend = await self.run_export([[self.raw], first, [], {"query": {"has_more": False}, "messages": []}])
        self.assertEqual(backend.call.call_args_list[2].args[1]["offset"], 1)
        self.assertEqual(self.report()["kinds"]["voice"]["records"], 1)
        self.assertTrue(self.report()["kinds"]["voice"]["pages"][-1]["full_agent_identity_match"])

    async def test_old_output_is_preserved(self):
        self.output.mkdir()
        original = self.output / "voice.jsonl"
        original.write_text("preserve", encoding="utf8")
        with self.assertRaises(FileExistsError):
            await self.run_export([])
        self.assertEqual(original.read_text(encoding="utf8"), "preserve")

    async def test_zero_server_id_matches_omitted_agent_id(self):
        self.raw["server_id_str"] = "0"
        self.agent["id"].pop("server_id_str")
        await self.run_export([[self.raw], {"query": {"has_more": False}, "messages": [self.agent]}])
        self.assertTrue(self.report()["complete"])

    async def test_resume_preserves_verified_pages_and_continues_cursor(self):
        first = {"query": {"has_more": True, "next_offset": 1}, "messages": [self.agent]}
        with self.assertRaisesRegex(RuntimeError, "Missing pagination"):
            await self.run_export([[self.raw], first, [], {"messages": []}])
        saved = (self.output / "voice.jsonl").read_bytes()
        self.options.resume = True
        backend = await self.run_export([[], {"query": {"has_more": False}, "messages": []}])
        self.assertEqual(backend.call.call_args_list[0].args[1]["offset"], 1)
        self.assertEqual((self.output / "voice.jsonl").read_bytes(), saved)
        self.assertTrue(self.report()["complete"])
        self.assertIn("Missing pagination", self.report()["resumed_attempts"][0]["previous_error"])

    async def test_resume_refuses_mismatched_query_and_untracked_rows(self):
        await self.run_export([[self.raw], {"query": {"has_more": False}, "messages": [self.agent]}])
        self.options.resume = True
        self.options.before = 101
        with self.assertRaisesRegex(ValueError, "Resume scope"):
            await self.run_export([])
        self.options.before = 100
        with (self.output / "voice.jsonl").open("a", encoding="utf8") as target:
            target.write(json.dumps(self.raw) + "\n")
        with self.assertRaisesRegex(ValueError, "count does not match"):
            await self.run_export([])


if __name__ == "__main__":
    unittest.main()
