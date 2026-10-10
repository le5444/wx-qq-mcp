"""Person/group request routing without real accounts or model calls."""
import json
import unittest

from unified_mcp.server import Gateway


class ChatEntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = Gateway()
        self.calls = []
        self.failed = None
        async def fetch(source, method, args):
            self.calls.append((source, method, args))
            if source == self.failed:
                raise RuntimeError("Synthetic unavailable platform")
            return {"candidates": [{"id": source + "-candidate", "name": "Same Name"}],
                    "complete": False, "truncated": True}
        self.gateway.fetch = fetch

    async def asyncTearDown(self):
        await self.gateway.close()

    async def resolve(self, **args):
        r = await self.gateway.call("unified_resolve_chat", args)
        return r, json.loads(r.content[0].text)

    async def test_wechat_only_does_not_probe_or_fail_on_unconfigured_qq(self):
        self.failed = "qq"
        r, data = await self.resolve(query="Alex", source="wechat")
        self.assertFalse(r.isError)
        self.assertEqual(list(data), ["wechat"])
        self.assertEqual([c[0] for c in self.calls], ["wechat"])

    async def test_qq_only_does_not_start_or_query_wechat(self):
        self.failed = "wechat"
        r, data = await self.resolve(query="Alex", source="qq")
        self.assertFalse(r.isError)
        self.assertEqual(list(data), ["qq"])
        self.assertEqual([c[:2] for c in self.calls], [("qq", "resolve_contact")])

    async def test_wechat_group_filter_reaches_reader(self):
        await self.resolve(query="Project group", source="wechat", wechat_type_filter="group", limit=25)
        self.assertEqual(self.calls, [("wechat", "resolve_chat", {"query": "Project group", "limit": 25, "type_filter": "group"})])

    async def test_two_platform_group_request_preserves_platform_identity(self):
        r, data = await self.resolve(query="Project group", wechat_type_filter="group", qq_chat_type="group")
        self.assertFalse(r.isError)
        self.assertEqual([c[:2] for c in self.calls], [("wechat", "resolve_chat"), ("qq", "resolve_group")])
        self.assertNotIn("same_person", data)
        self.assertNotEqual(data["wechat"]["candidates"][0]["id"], data["qq"]["candidates"][0]["id"])

    async def test_both_default_retains_available_candidates_but_reports_failure(self):
        self.failed = "qq"
        r, data = await self.resolve(query="Alex")
        self.assertTrue(r.isError)
        self.assertTrue(data["wechat"]["candidates"])
        self.assertIn("error", data["qq"])

    async def test_truncation_survives_discovery_and_is_not_uniqueness_proof(self):
        _, data = await self.resolve(query="Alex", source="qq", limit=1)
        self.assertTrue(data["qq"]["truncated"])
        self.assertFalse(data["qq"]["complete"])
        self.assertNotIn("selected", data["qq"])

    async def test_invalid_or_conflicting_options_fail_before_reading(self):
        for args in ({"source": "wecaht"}, {"wechat_type_filter": "groups"},
                     {"qq_chat_type": "discuss"}, {"limit": True}, {"limit": 101},
                     {"source": "qq", "wechat_type_filter": "group"},
                     {"source": "wechat", "qq_chat_type": "group"}, {"query": "  "}):
            with self.subTest(args=args):
                r, _ = await self.resolve(**{"query": "Alex", **args})
                self.assertTrue(r.isError)
                self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
