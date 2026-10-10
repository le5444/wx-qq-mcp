import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from unified_mcp.wechat_media import WeChatMediaResolver, _identity


def png():
    out = io.BytesIO()
    Image.new("RGB", (16, 13), "purple").save(out, format="PNG")
    return out.getvalue()


def v4(plain, key=b"cfcd208495d565ef", xor=87):
    aes_len = min(32, len(plain))
    prefix = plain[:aes_len]
    pad = 16 - aes_len % 16
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(prefix + bytes([pad]) * pad) + encryptor.finalize()
    tail = plain[aes_len:]
    return b"\x07\x08V1\x08\x07" + struct.pack("<II", aes_len, len(tail)) + b"\0" + encrypted + bytes(x ^ xor for x in tail)


class WeChatMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test-media-")
        # Compare canonical filesystem paths, not short-name spellings.
        self.root = Path(self.tmp.name).resolve()
        runtime_patch = patch("unified_mcp.wechat_media.RUNTIME", self.root)
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)
        self.data = self.root / "data"
        self.account = self.data / "wxid_self_1234"
        self.account.mkdir(parents=True)
        self.resolver = WeChatMediaResolver(account_root=self.account, data_root=self.data,
            metadata_dir=self.root / "metadata", output_dir=self.root / "out", config_path=self.root / "none.json")
        self.md5 = hashlib.md5(png()).hexdigest()
        self.row = {"talker": "wxid_friend", "server_id_str": "123", "local_id": 5,
                    "create_time": 1700000000, "kind_name": "image",
                    "message_content_parsed": {"md5": self.md5}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_md5_content_index_reuses_existing_file(self):
        folder = self.data / "Images/wxid_friend/2023-11"
        folder.mkdir(parents=True)
        path = folder / "arbitrary_name.png"
        path.write_bytes(png())
        # Newly booted Windows runners can have monotonic uptime below the TTL.
        with patch("unified_mcp.wechat_media.time.monotonic", return_value=100):
            result = self.resolver.resolve(self.row)
        self.assertEqual(result["status"], "readable")
        self.assertEqual(result["images"][0]["path"], str(path))
        self.assertTrue(result["images"][0]["xml_md5_matches"])
        self.assertEqual(self.resolver.new_bytes, 0)

    def test_emoji_mismatched_filename_not_used(self):
        folder = self.data / "Emojis"
        folder.mkdir()
        (folder / ("f" * 32 + ".png")).write_bytes(png())
        row = {**self.row, "kind_name": "sticker", "message_content_parsed": {"md5": "f" * 32, "cdn_url": "https://example.invalid"}}
        result = self.resolver.resolve(row)
        self.assertEqual(result["status"], "local_file_missing")
        self.assertTrue(result["remote_reference_present"])
        self.assertFalse(result["network_used"])

    def test_animated_sticker_complete_decode(self):
        folder = self.data / "Emojis"; folder.mkdir()
        out = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(out, format="GIF", save_all=True,
            append_images=[Image.new("RGB", (8, 8), "blue")])
        data = out.getvalue(); digest = hashlib.md5(data).hexdigest()
        (folder / (digest + ".gif")).write_bytes(data)
        row = {**self.row, "kind_name": "sticker", "message_content_parsed": {"md5": digest}}
        with patch("unified_mcp.wechat_media.time.monotonic", return_value=100):
            result = self.resolver.resolve(row)
        self.assertEqual(result["images"][0]["media_validation"]["frames"], 2)

    def test_v4_direct_trailer_derivation_and_source_unchanged(self):
        dat = self.root / "image.dat"; data = v4(png());dat.write_bytes(data)
        result, status = self.resolver._decode_dat(dat, self.md5)
        self.assertEqual(status, "decoded")
        self.assertEqual(result[0][0], png())
        self.assertEqual(dat.read_bytes(), data)

    def test_truncated_image_header_is_not_readable(self):
        image = self.root / "bad.jpg"; image.write_bytes(b"\xff\xd8\xff" + b"bad data")
        self.assertIsNone(self.resolver._ref(image, "test"))

    def test_v4_invalid_lengths_fail_closed(self):
        dat = self.root / "image.dat"
        dat.write_bytes(b"\x07\x08V1\x08\x07" + struct.pack("<II", 1000000, 1000000) + b"\0")
        self.assertEqual(self.resolver._decode_dat(dat)[1], "invalid_dat_lengths")

    def test_multiple_decodable_candidates_are_rejected(self):
        dat = self.root / "image.dat"; dat.write_bytes(v4(png()))
        self.resolver._xor.update({0, 1})
        with patch("unified_mcp.wechat_media._decode_bytes", return_value={"valid": True}):
            self.assertEqual(self.resolver._decode_dat(dat)[1], "ambiguous_decoding")
        self.assertFalse(self.resolver.output.exists())

    def test_metadata_injected_with_exact_identity(self):
        agent = {"id": {"talker": "wxid_friend", "server_id_str": "123", "local_id": 5},
                 "create_time": 1700000000, "kind": "image"}
        output = self.resolver.enrich_payload({"messages": [agent]}, metadata_rows=[self.row])
        self.assertNotEqual(output["messages"][0]["wechat_media_resolution"]["status"], "metadata_missing")
        other = {**agent, "create_time": 1700000001}
        output = self.resolver.enrich_payload({"messages": [other]})
        self.assertEqual(output["messages"][0]["wechat_media_resolution"]["status"], "metadata_missing")
        self.assertNotIn("wechat_media_resolution", agent)

    def test_resources_require_local_id_and_time_match(self):
        wrong = {**self.row, "local_id": 6, "resources": [{"md5": "a" * 32}]}
        self.resolver.enrich_payload({}, resource_rows=[wrong])
        self.assertNotIn(_identity(self.row), self.resolver._resources)

    def test_zero_server_id_matches_omitted_agent_server_id(self):
        raw = {**self.row, "server_id_str": "0", "server_id": 0}
        agent = {"id": {"talker": "wxid_friend", "local_id": 5}, "create_time": 1700000000}
        self.assertEqual(_identity(raw), _identity(agent))

    def test_native_image_can_be_reused_without_full_xml(self):
        path = self.root / "existing.png"; path.write_bytes(png())
        agent = {"id": {"talker": "wxid_friend", "local_id": 5}, "create_time": 1700000000,
                 "kind": "image", "images": [{"path": str(path)}]}
        result = self.resolver.resolve(agent)
        self.assertEqual(result["status"], "readable")
        self.assertEqual(result["images"][0]["provenance"], "existing_backend_reference")

    def test_empty_xml_md5_still_resolves_exact_resource_dat(self):
        row = {**self.row, "message_content_parsed": {"md5": ""}}
        resource = {**row, "resources": [{"md5": "a" * 32}]}
        folder = self.account / "msg/attach" / hashlib.md5(b"wxid_friend").hexdigest() / "2023-11/Img"
        folder.mkdir(parents=True)
        (folder / ("a" * 32 + ".dat")).write_bytes(v4(png()))
        with patch("unified_mcp.wechat_media.time.monotonic", return_value=100):
            result = self.resolver.enrich_payload(row, resource_rows=[resource])
        self.assertEqual(result["wechat_media_resolution"]["status"], "readable")
        self.assertFalse(result["wechat_media_resolution"]["content_md5_available"])
        self.assertFalse(result["images"][0]["xml_md5_matches"])

    def test_cold_uptime_caches_only_after_scanning_and_refreshes_at_ttl(self):
        with patch("unified_mcp.wechat_media.time.monotonic", return_value=100) as clock, \
             patch("unified_mcp.wechat_media._files", return_value=[]) as files:
            self.resolver._index("wxid_friend")
            first_scans = files.call_count
            self.assertGreater(first_scans, 0)
            self.assertEqual(self.resolver._global_indexed, 100)
            self.assertEqual(self.resolver._indexed_chats["wxid_friend"], 100)
            clock.return_value = 399
            self.resolver._index("wxid_friend")
            self.assertEqual(files.call_count, first_scans)
            clock.return_value = 400
            self.resolver._index("wxid_friend")
            self.assertEqual(files.call_count, first_scans * 2)
            self.assertEqual(self.resolver._global_indexed, 400)
            self.assertEqual(self.resolver._indexed_chats["wxid_friend"], 400)

    def test_zero_is_a_valid_cached_timestamp_and_new_chat_still_scans(self):
        with patch("unified_mcp.wechat_media.time.monotonic", return_value=0) as clock, \
             patch("unified_mcp.wechat_media._files", return_value=[]) as files:
            self.resolver._index("wxid_friend")
            first_scans = files.call_count
            self.assertGreater(first_scans, 0)
            self.resolver._index("wxid_friend")
            self.assertEqual(files.call_count, first_scans)
            clock.return_value = 100
            self.resolver._index("wxid_another_friend")
            self.assertGreater(files.call_count, first_scans)
            self.assertEqual(self.resolver._global_indexed, 0)
            self.assertEqual(self.resolver._indexed_chats["wxid_another_friend"], 100)

    def test_output_deduplicated_and_budget_enforced(self):
        from unified_mcp.media_validation import _decode_bytes
        data = png(); verdict = _decode_bytes(data)
        self.resolver.max_new_bytes = len(data) - 1
        self.assertEqual(self.resolver._save(data, verdict)[1], "output_budget_reached")
        self.resolver.max_new_bytes = len(data)
        first, _ = self.resolver._save(data, verdict)
        second, status = self.resolver._save(data, verdict)
        self.assertEqual(first, second)
        self.assertEqual(status, "reused_recovered_copy")
        self.assertEqual(self.resolver.new_bytes, len(data))

    def test_source_data_not_changed_when_recovery_fails(self):
        dat = self.root / "broken.dat"; dat.write_bytes(b"bad")
        before = dat.read_bytes()
        self.assertEqual(self.resolver._decode_dat(dat)[1], "decode_failed")
        self.assertEqual(before, dat.read_bytes())


if __name__ == "__main__":
    unittest.main()
