import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from mcp import types
from PIL import Image

from unified_mcp.server import Gateway


class FakeWeChat:
    def __init__(self, result):
        self.result = result

    async def call(self, *_):
        return self.result

    async def close(self):
        pass


class GatewayMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_media_failure_preserves_message_page(self):
        data = {"messages": [{"kind": "text", "text": "preserve this message"}], "query": {"has_more": False}}
        result = types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data))])
        gateway = Gateway()
        gateway.wechat = FakeWeChat(result)
        gateway._enrich_wechat = AsyncMock(side_effect=OSError("cache unavailable"))
        try:
            returned = await gateway.call("chat_timeline", {})
            self.assertFalse(returned.isError)
            page = json.loads(returned.content[0].text)
            self.assertEqual(page["messages"], data["messages"])
            self.assertTrue(page["warnings"])
        finally:
            await gateway.close()

    async def test_metadata_only_resource_call_does_not_start_media_processing(self):
        result = types.CallToolResult(content=[types.TextContent(type="text", text="[]")])
        gateway = Gateway()
        gateway.wechat = FakeWeChat(result)
        gateway._enrich_wechat = AsyncMock(side_effect=AssertionError("must not enrich metadata-only query"))
        try:
            self.assertIs(await gateway.fetch("wechat", "media_resources", {"include_local_paths": False}), result)
            gateway._enrich_wechat.assert_not_awaited()
        finally:
            await gateway.close()

    async def test_native_result_and_structured_result_both_reject_broken_jpeg(self):
        with tempfile.TemporaryDirectory() as folder:
            valid = Path(folder) / "valid.png"
            Image.new("RGB", (8, 8)).save(valid)
            broken = Path(folder) / "broken.jpg"
            broken.write_bytes(b"\xff\xd8\xff\xe1invalid-jpeg")
            data = {"messages": [{"images": [{"path": str(valid)}, {"path": str(broken)}]}]}
            result = types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data))], structuredContent=data)
            gateway = Gateway()
            gateway.wechat = FakeWeChat(result)
            try:
                returned = await gateway.fetch("wechat", "chat_timeline", {})
                for payload in (json.loads(returned.content[0].text), returned.structuredContent):
                    images = payload["messages"][0]["images"]
                    self.assertEqual(images[0]["path"], str(valid))
                    self.assertNotIn("path", images[1])
                    self.assertEqual(images[1]["unavailable_path"], str(broken))
                self.assertIn("path", data["messages"][0]["images"][1])
            finally:
                await gateway.close()

    async def test_backend_error_is_preserved(self):
        result = types.CallToolResult(content=[types.TextContent(type="text", text="read failed")], isError=True)
        gateway = Gateway()
        gateway.wechat = FakeWeChat(result)
        try:
            self.assertIs(await gateway.fetch("wechat", "chat_timeline", {}), result)
        finally:
            await gateway.close()
