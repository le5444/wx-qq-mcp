import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unified_mcp.video_media import WeChatVideoResolver, _ffmpeg_info, probe_video, preview_frames


VIDEO_LOG = """Duration: 00:00:06.30, start: 0.000000, bitrate: 1927 kb/s
Stream #0:0[0x1](und): Video: h264 (Main) (avc1 / 0x31637661), yuv420p(tv, bt709), 720x1280, 30 fps
frame=    1 fps=0.0 q=-0.0 Lsize=N/A
"""


class VideoMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test-video-")
        # Compare canonical filesystem paths, not short-name spellings.
        self.root = Path(self.tmp.name).resolve()
        runtime_patch = patch("unified_mcp.video_media.RUNTIME", self.root)
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)
        self.account = self.root / "wxid_self_1234"
        self.metadata = self.root / "metadata"; self.metadata.mkdir()
        self.row = {"talker": "wxid_friend", "server_id_str": "123", "local_id": 8,
                    "create_time": 1700000000, "kind_name": "video"}
        self.resource = {**self.row, "resources": [{"resource_family": "video", "md5": "a" * 32}]}
        (self.metadata / "video.jsonl").write_text(json.dumps(self.row) + "\n", encoding="utf-8")
        (self.metadata / "video_resources.jsonl").write_text(json.dumps(self.resource) + "\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def resolver(self):
        return WeChatVideoResolver(account_root=self.account, metadata_dir=self.metadata, config_path=self.root / "none.json")

    def test_ffmpeg_stream_info_reports_codec_duration_and_dimensions(self):
        self.assertEqual(_ffmpeg_info(VIDEO_LOG), {"codec": "h264", "width": 720, "height": 1280, "duration_seconds": 6.3})

    def test_audio_only_or_missing_duration_not_video(self):
        self.assertIsNone(_ffmpeg_info("Duration: 00:00:03.00\nStream #0:0: Audio: aac"))
        self.assertIsNone(_ffmpeg_info("Stream #0:0: Video: h264, yuv420p, 720x1280"))

    def test_header_without_decoded_frames_fails(self):
        path = self.root / "bad.mp4"; path.write_bytes(b"not a movie")
        mock = SimpleNamespace(returncode=0, stdout="", stderr=VIDEO_LOG.replace("frame=    1", "frame=    0"))
        with patch("unified_mcp.video_media._binary", return_value=None), patch("unified_mcp.video_media._run", return_value=(mock, None)):
            result = probe_video(path, ffmpeg="mock-ffmpeg")
        self.assertFalse(result["valid"])

    def test_successful_probe_only_claims_first_frame(self):
        path = self.root / "movie.mp4"; path.write_bytes(b"fixture")
        before = path.read_bytes()
        mock = SimpleNamespace(returncode=0, stdout="", stderr=VIDEO_LOG)
        with patch("unified_mcp.video_media._binary", return_value=None), patch("unified_mcp.video_media._run", return_value=(mock, None)):
            result = probe_video(path, ffmpeg="mock-ffmpeg")
        self.assertTrue(result["valid"])
        self.assertFalse(result["whole_video_decoded"])
        self.assertEqual(result["probe_method"], "ffmpeg_first_frame")
        self.assertEqual(before, path.read_bytes())

    def test_ffprobe_uses_container_duration_when_stream_duration_unknown(self):
        path = self.root / "movie.mp4"; path.write_bytes(b"fixture")
        metadata = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "streams": [{"codec_name": "h264", "width": 720, "height": 1280, "duration": "N/A"}],
            "format": {"duration": "6.3"}}))
        decoded = SimpleNamespace(returncode=0, stdout="", stderr=VIDEO_LOG)
        with patch("unified_mcp.video_media._run", side_effect=[(metadata, None), (decoded, None)]):
            result = probe_video(path, ffprobe="mock-ffprobe", ffmpeg="mock-ffmpeg")
        self.assertTrue(result["valid"])
        self.assertEqual(result["duration_seconds"], 6.3)
        self.assertEqual(result["probe_method"], "ffprobe_and_first_frame")

    def test_missing_source_distinct_from_missing_metadata(self):
        resolver = self.resolver()
        self.assertEqual(resolver.resolve(self.row)["status"], "source_missing")
        self.assertEqual(resolver.resolve({**self.row, "local_id": 9})["status"], "metadata_missing")

    def test_exact_resource_id_and_account_scoping(self):
        folder = self.account / "msg/video/2023-11"; folder.mkdir(parents=True)
        path = folder / ("a" * 32 + ".mp4"); path.write_bytes(b"fixture")
        sibling = folder / ("b" * 32 + ".mp4"); sibling.write_bytes(b"other")
        verdict = {"valid": True, "duration_seconds": 6.3, "width": 720, "height": 1280}
        with patch("unified_mcp.video_media.probe_video", return_value=verdict) as probe:
            result = self.resolver().resolve(self.row)
        self.assertEqual(result["status"], "playable_local")
        self.assertEqual(result["videos"][0]["path"], str(path))
        self.assertEqual(probe.call_count, 1)

    def test_thumbnail_alone_is_not_a_playable_video(self):
        from PIL import Image
        folder = self.account / "msg/video/2023-11"; folder.mkdir(parents=True)
        Image.new("RGB", (8, 8)).save(folder / ("a" * 32 + "_thumb.jpg"))
        result = self.resolver().resolve(self.row)
        self.assertEqual(result["status"], "source_missing")
        self.assertEqual(len(result["cover_paths"]), 1)
        self.assertEqual(result["videos"], [])

    def test_previews_refuse_output_outside_runtime(self):
        with self.assertRaises(ValueError):
            preview_frames(self.root / "movie.mp4", {"valid": True}, output_dir=Path.home(), ffmpeg="mock-ffmpeg")

    def test_enrichment_keeps_input_unchanged(self):
        row = {"id": {"talker": "wxid_friend", "local_id": 8, "server_id_str": "123"},
               "create_time": 1700000000, "kind": "video"}
        result = self.resolver().enrich_payload({"messages": [row]})
        self.assertEqual(result["messages"][0]["wechat_video_resolution"]["status"], "source_missing")
        self.assertNotIn("wechat_video_resolution", row)


if __name__ == "__main__":
    unittest.main()
