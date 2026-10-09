import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import urllib.error

from PIL import Image

from unified_mcp.sticker_download import StickerDownloader, allowed_host, FetchFailure, missing_groups, run


class Response(io.BytesIO):
    def __init__(self, data=b"", status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers if headers is not None else {"Content-Length": str(len(data))}


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class StickerDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch("unified_mcp.sticker_download.RUNTIME", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        image = Image.new("RGB", (3, 2), "red")
        data = io.BytesIO()
        image.save(data, format="GIF", save_all=True, append_images=[Image.new("RGB", (3, 2), "blue")])
        self.data = data.getvalue()
        self.md5 = hashlib.md5(self.data).hexdigest()
        self.url = "https://wxapp.tc.qq.com/resource?private_token=do-not-log"

    def downloader(self, *responses, **kwargs):
        self.opener = Opener(responses)
        return StickerDownloader(self.root / "download", opener=self.opener,
                                 resolver=lambda *a, **k: [(2, 1, 6, "", ("1.2.3.4", 443))], **kwargs)

    def test_matching_animation_is_saved_without_tokens_or_credentials(self):
        result = self.downloader(Response(self.data)).download(self.md5, self.url)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["frames"], 2)
        self.assertEqual(Path(result["path"]).read_bytes(), self.data)
        self.assertNotIn("do-not-log", json.dumps(result))
        request = self.opener.requests[0][0]
        self.assertEqual(request.method, "GET")
        self.assertIsNone(request.data)
        self.assertNotIn("Cookie", request.headers)
        self.assertNotIn("Authorization", request.headers)

    def test_nonofficial_credentials_ip_and_nonhttp_urls_are_blocked(self):
        for url in ["http://127.0.0.1/x", "http://localhost/x", "file:///tmp/x", "https://qq.com.evil.test/x",
                    "https://user:password@wxapp.tc.qq.com/x", "https://wxapp.tc.qq.com:666/x"]:
            with self.subTest(url=url), self.assertRaises(FetchFailure):
                allowed_host(url)

    def test_private_dns_address_is_blocked(self):
        downloader = self.downloader()
        downloader.resolver = lambda *a, **k: [(2, 1, 6, "", ("192.168.1.1", 80))]
        result = downloader.download(self.md5, self.url)
        self.assertEqual(result["status"], "blocked_address")
        self.assertEqual(self.opener.requests, [])

    def test_explicit_tun_mode_requires_https_and_still_blocks_private_addresses(self):
        downloader = self.downloader(Response(self.data), https_tun=True)
        downloader.resolver = lambda *a, **k: [(2, 1, 6, "", ("198.18.0.9", 443)), (10, 1, 6, "", ("2001:2::6e", 443))]
        result = downloader.download(self.md5, self.url.replace("https:", "http:"))
        self.assertEqual(result["status"], "available")
        self.assertTrue(result["https_upgrade"])
        self.assertTrue(self.opener.requests[0][0].full_url.startswith("https://"))
        downloader = self.downloader(https_tun=True)
        downloader.resolver = lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))]
        self.assertEqual(downloader.download("b" * 32, self.url)["status"], "blocked_address")

    def test_explicit_original_tun_mode_preserves_source_http(self):
        downloader = self.downloader(Response(self.data), original_http_tun=True)
        downloader.resolver = lambda *a, **k: [(2, 1, 6, "", ("198.18.0.9", 80))]
        result = downloader.download(self.md5, self.url.replace("https:", "http:"))
        self.assertEqual(result["status"], "available")
        self.assertTrue(self.opener.requests[0][0].full_url.startswith("http://"))
        self.assertNotIn("https_upgrade", result)

    def test_explicit_xml_key_decrypts_in_memory_before_md5_and_full_decode(self):
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        key = "1234567890abcdef1234567890abcdef"
        encrypted = AES.new(bytes.fromhex(key), AES.MODE_ECB).encrypt(pad(self.data, 16))
        result = self.downloader(Response(encrypted)).download(self.md5, self.url, aes_key=key)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["decryption"], "AES-128-ECB-PKCS7")
        self.assertEqual(Path(result["path"]).read_bytes(), self.data)
        self.assertNotIn(key, json.dumps(result))

    def test_wrong_xml_key_does_not_publish_invalid_plaintext(self):
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        encrypted = AES.new(bytes.fromhex("1" * 32), AES.MODE_ECB).encrypt(pad(self.data, 16))
        result = self.downloader(Response(encrypted)).download(self.md5, self.url, aes_key="2" * 32)
        self.assertEqual(result["status"], "decrypted_md5_mismatch")
        self.assertEqual(list((self.root / "download").glob("*")), [])

    def test_legacy_emoji_cbc_requires_matching_plaintext_md5(self):
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        key = bytes.fromhex("1234567890abcdef1234567890abcdef")
        encrypted = AES.new(key, AES.MODE_CBC, iv=key).encrypt(pad(self.data, 16))
        result = self.downloader(Response(encrypted)).download(self.md5, self.url, aes_key=key.hex())
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["decryption"], "AES-128-CBC-IV=XML-key-PKCS7")
        self.assertEqual(Path(result["path"]).read_bytes(), self.data)

    def test_each_redirect_is_checked_before_any_followup_request(self):
        downloader = self.downloader(Response(status=302, headers={"Location": "http://127.0.0.1/secret"}))
        result = downloader.download(self.md5, self.url)
        self.assertEqual(result["status"], "blocked_url")
        self.assertEqual(len(self.opener.requests), 1)

    def test_official_redirect_and_http_error_redirect_are_allowed(self):
        error = urllib.error.HTTPError(self.url, 302, "redirect", {"Location": "https://mmbiz.qpic.cn/x"}, None)
        result = self.downloader(error, Response(self.data)).download(self.md5, self.url)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["source_host"], "mmbiz.qpic.cn")

    def test_digest_mismatch_and_corrupt_bytes_are_never_published(self):
        result = self.downloader(Response(self.data)).download("a" * 32, self.url)
        self.assertEqual(result["status"], "md5_mismatch")
        broken = self.data[:20]
        result = self.downloader(Response(broken)).download(hashlib.md5(broken).hexdigest(), self.url)
        self.assertEqual(result["status"], "image_validation_failed")
        self.assertEqual(list((self.root / "download").glob("*")), [])

    def test_file_limit_and_shared_total_budget(self):
        result = self.downloader(Response(self.data), max_file_bytes=10).download(self.md5, self.url)
        self.assertEqual(result["status"], "file_byte_limit")
        downloader = self.downloader(Response(self.data), max_total_bytes=50)
        result = downloader.download(self.md5, self.url)
        self.assertEqual(result["status"], "total_byte_limit")
        self.assertLessEqual(downloader.used_bytes, 50)

    def test_stream_without_length_stops_at_budget(self):
        downloader = self.downloader(Response(b"x" * 1000, headers={}), max_total_bytes=50)
        result = downloader.download(self.md5, self.url)
        self.assertEqual(result["status"], "total_byte_limit")
        self.assertEqual(result["network_bytes"], 50)

    def test_failures_have_one_attempt_and_no_sensitive_error_message(self):
        error = urllib.error.HTTPError(self.url, 403, "private_token=do-not-log", {}, None)
        result = self.downloader(error).download(self.md5, self.url)
        self.assertEqual(result["status"], "http_error")
        self.assertEqual(result["http_status"], 403)
        self.assertEqual(len(self.opener.requests), 1)
        self.assertNotIn("do-not-log", json.dumps(result))

    def test_existing_file_is_validated_and_invalid_cache_is_never_overwritten(self):
        first = self.downloader(Response(self.data)).download(self.md5, self.url)
        result = self.downloader().download(self.md5, self.url)
        self.assertEqual(result["status"], "available")
        self.assertTrue(result["cache_hit"])
        self.assertEqual(self.opener.requests, [])
        path = Path(first["path"])
        path.write_bytes(b"preserve")
        result = self.downloader().download(self.md5, self.url)
        self.assertEqual(result["status"], "existing_cache_conflict")
        self.assertEqual(path.read_bytes(), b"preserve")

    def test_missing_groups_are_exact_identity_scoped_and_deduplicated(self):
        row = {"talker": "target", "server_id_str": "1", "local_id": 2, "create_time": 3,
               "message_content_parsed": {"md5": self.md5, "cdn_url": self.url}}
        same_hash = {**row, "server_id_str": "2", "local_id": 4}
        unrelated = {**row, "talker": "different"}
        resolved = [{"identity": ["target", "1", "2", "3"], "status": "local_file_missing"},
                    {"identity": ["target", "2", "4", "3"], "status": "local_file_missing"}]
        groups = missing_groups([row, same_hash, unrelated], resolved)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["identities"]), 2)

    def test_cli_ledger_does_not_retry_previous_failure(self):
        raw = self.root / "raw.jsonl"
        resolved = self.root / "resolved.jsonl"
        raw.write_text(json.dumps({"talker": "target", "server_id_str": "1", "local_id": 2,
                                   "create_time": 3, "message_content_parsed": {"md5": self.md5, "cdn_url": self.url}}) + "\n")
        resolved.write_text(json.dumps({"identity": ["target", "1", "2", "3"], "status": "local_file_missing"}) + "\n")
        downloader = self.downloader(urllib.error.HTTPError(self.url, 404, "missing", {}, None))
        options = SimpleNamespace(metadata=raw, resolved=resolved, output=self.root / "download", limit=None)
        with patch("unified_mcp.sticker_download.StickerDownloader", return_value=downloader):
            first = run(options)
            second = run(options)
        self.assertTrue(first["complete"] and second["complete"])
        self.assertEqual(len(self.opener.requests), 1)
        self.assertEqual(second["statuses"], {"http_error": 1})

    def test_explicit_legacy_correction_is_bounded_to_one_new_request(self):
        raw = self.root / "raw.jsonl"
        resolved = self.root / "resolved.jsonl"
        raw.write_text(json.dumps({"talker": "target", "server_id_str": "1", "local_id": 2,
                                  "create_time": 3, "message_content_parsed": {
                                      "md5": self.md5, "encrypt_url": self.url, "aeskey": "1" * 32}}) + "\n")
        resolved.write_text(json.dumps({"identity": ["target", "1", "2", "3"], "status": "local_file_missing"}) + "\n")
        downloader = self.downloader(Response(b"x" * 32))
        downloader.output.mkdir()
        ledger = downloader.output / "download_results.jsonl"
        ledger.write_text(json.dumps({"md5": self.md5, "status": "encrypted_payload_invalid",
                                      "network_used": True, "requests": 1, "network_bytes": 32}) + "\n")
        options = SimpleNamespace(metadata=raw, resolved=resolved, output=downloader.output, limit=None,
                                  legacy_sticker_cbc_correction=True)
        with patch("unified_mcp.sticker_download.StickerDownloader", return_value=downloader):
            run(options)
            final = run(options)
        self.assertEqual(len(self.opener.requests), 1)
        self.assertEqual(final["statuses"], {"decrypted_md5_mismatch": 1})
        self.assertEqual(final["explicit_legacy_emoji_corrections"], 1)


if __name__ == "__main__":
    unittest.main()
