"""Public installs must load safely without borrowing the developer's account."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class PortabilityTests(unittest.TestCase):
    def run_isolated(self, code, extra=None):
        with tempfile.TemporaryDirectory(prefix="wxqq-config-test-") as directory:
            root = Path(directory)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("QQ_MCP_", "QQNT_", "NTQQ_", "WX_UNIFIED_", "UNIFIED_", "WXQQ_"))}
            env.update(PYTHONPATH=str(Path(__file__).resolve().parent.parent),
                       WXQQ_DATA_DIR=str(root / "data"), PYTHONDONTWRITEBYTECODE="1",
                       PYTHONIOENCODING="utf-8", QQ_MCP_DB_ROOT="")
            env.update(extra or {})
            result = subprocess.run([sys.executable, "-c", code], env=env, cwd=root,
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout), (root / "data").exists()

    def test_catalog_and_diagnosis_work_without_account_or_runtime_writes(self):
        value, wrote = self.run_isolated('''
import asyncio, json
from unified_mcp.server import Gateway
import qq_mcp_server
async def main():
    gateway = Gateway()
    try:
        tools = gateway.tools()
        diagnose = await gateway.call("qq_diagnose", {})
        data = json.loads(diagnose.content[0].text)
        query = await gateway.call("qq_messages", {"contact": "unconfigured"})
        assert qq_mcp_server.DEFAULT_DB_ROOT is None
        assert not data["db_root_configured"] and not data["ready"]
        assert query.isError and "QQ_MCP_DB_ROOT" in query.content[0].text
        assert not gateway.wechat.active and gateway.wechat.starts == 0
        print(json.dumps({"count": len(tools), "unique": len({t.name for t in tools})}))
    finally:
        await gateway.close()
asyncio.run(main())
''')
        self.assertEqual(value, {"count": 40, "unique": 40})
        self.assertFalse(wrote)

    def test_all_default_gateway_caches_live_outside_the_package(self):
        value, wrote = self.run_isolated('''
import json
from pathlib import Path
from unified_mcp.runtime_paths import RUNTIME
from unified_mcp.voice_transcription import CACHE, ASR_HOME
from unified_mcp.image_text import IMAGE_TEXT_RUNTIME
from unified_mcp.sticker_download import RUNTIME as sticker_runtime
from unified_mcp.media_validation import RUNTIME as repair_runtime
import qq_mcp_server as qq
paths = [CACHE, ASR_HOME, IMAGE_TEXT_RUNTIME, sticker_runtime, repair_runtime,
         qq.DEFAULT_KEY_FILE, qq.CACHE_DIR]
assert all(path.resolve().is_relative_to(RUNTIME) for path in paths)
assert not RUNTIME.is_relative_to(Path(qq.__file__).resolve().parent)
print(json.dumps({"verified": len(paths)}))
''')
        self.assertEqual(value["verified"], 7)
        self.assertFalse(wrote)

    def test_explicit_voice_runtime_paths_are_honored(self):
        overrides = {"WX_UNIFIED_ASR_HOME": "custom-asr", "WX_UNIFIED_ASR_PYTHON": "custom-python",
                     "WX_UNIFIED_ASR_MODEL": "custom-whisper", "WX_UNIFIED_SENSE_MODEL": "custom-sense",
                     "WX_UNIFIED_VOICE_CACHE": "custom-cache", "WX_UNIFIED_VOICE_MEDIA_CACHE": "custom-media"}
        value, wrote = self.run_isolated('''
import json
from unified_mcp.voice_transcription import ASR_HOME, PYTHON, MODEL, SENSE_MODEL, CACHE, MEDIA_CACHE
print(json.dumps([str(p) for p in (ASR_HOME, PYTHON, MODEL, SENSE_MODEL, CACHE, MEDIA_CACHE)]))
''', overrides)
        self.assertEqual(value, list(overrides.values()))
        self.assertFalse(wrote)

    def test_raw_key_configuration_is_visible_to_readiness_check(self):
        value, wrote = self.run_isolated('''
import json
from pathlib import Path
from unittest.mock import patch
from unified_mcp import qq_adapter as adapter
with patch.object(adapter.legacy, "DEFAULT_DB_ROOT", Path("selected-account")), \
     patch.object(adapter, "key_map", return_value={"mock-salt": "mock-key"}), \
     patch.object(adapter, "_original_key_status", side_effect=AssertionError("legacy key not needed")):
    source, ready = adapter.legacy.get_key_status()
    assert source.endswith("raw-keys.dpapi")
    print(json.dumps({"configured": ready}))
''')
        self.assertTrue(value["configured"])
        self.assertFalse(wrote)


if __name__ == "__main__":
    unittest.main()
