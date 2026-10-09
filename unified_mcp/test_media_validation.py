import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageFile

from unified_mcp import media_validation as media


class MediaValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.good = self.root / "good.jpg"
        Image.new("RGB", (120, 80), "purple").save(self.good)
        self.bad = self.root / "bad.jpg"
        self.bad.write_bytes(b"\xff\xd8\xff" + b"not a complete JPEG")

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_pixels_decoded(self):
        result = media.validate_image_path(self.good)
        self.assertTrue(result["valid"])
        self.assertEqual((result["width"], result["height"], result["format"]), (120, 80, "JPEG"))

    def test_jpeg_magic_does_not_pass(self):
        self.assertEqual(media.validate_image_path(self.bad)["status"], "decode_failed")

    def test_decodable_header_with_truncated_pixels_fails(self):
        broken = self.root / "truncated.jpg"
        broken.write_bytes(self.good.read_bytes()[:-30])
        with Image.open(broken) as header:
            self.assertEqual(header.size, (120, 80))
        self.assertFalse(media.validate_image_path(broken)["valid"])

    def test_extension_does_not_determine_image_format(self):
        png = self.root / "actually_png.jpg"
        Image.new("RGB", (5, 4)).save(png, format="PNG")
        self.assertEqual(media.validate_image_path(png)["format"], "PNG")

    def test_missing_directory_empty_and_remote_distinct(self):
        self.assertEqual(media.validate_image_path(self.root / "missing.jpg")["status"], "missing")
        self.assertEqual(media.validate_image_path(self.root)["status"], "not_file")
        empty = self.root / "empty.jpg"
        empty.touch()
        self.assertEqual(media.validate_image_path(empty)["status"], "empty")
        self.assertEqual(media.validate_image_path("https://example.com/image.jpg")["status"], "invalid_path")

    def test_file_uri_is_decoded(self):
        self.assertTrue(media.validate_image_path(self.good.as_uri())["valid"])

    def test_pillow_missing_fails_closed(self):
        with patch.object(media, "Image", None):
            self.assertEqual(media.validate_image_path(self.good)["status"], "decoder_unavailable")

    def test_permissive_global_decoder_fails_closed(self):
        with patch.object(ImageFile, "LOAD_TRUNCATED_IMAGES", True):
            self.assertEqual(media.validate_image_path(self.good)["status"], "decoder_not_strict")
        self.assertFalse(ImageFile.LOAD_TRUNCATED_IMAGES)

    def test_size_bound(self):
        with patch.object(media, "MAX_FILE_BYTES", 1):
            self.assertEqual(media.validate_image_path(self.good)["status"], "size_limit")

    def test_all_animation_frames_are_loaded(self):
        frames = [Image.new("RGB", (8, 8), color) for color in ("blue", "red", "green")]
        gif = self.root / "animated.gif"
        frames[0].save(gif, save_all=True, append_images=frames[1:])
        self.assertEqual(media.validate_image_path(gif)["frames"], 3)

    def test_recursive_payload_copy_removes_bad_paths_and_uri(self):
        source = {"messages": [{"kind": "image", "images": [
            {"path": str(self.good), "uri": self.good.as_uri()},
            {"path": str(self.bad), "uri": self.bad.as_uri(), "bytes": self.bad.stat().st_size},
        ], "quote": {"images": [{"path": str(self.bad)}]}}]}
        original = copy.deepcopy(source)
        cleaned = media.sanitize_media_payload(source)
        self.assertEqual(source, original)
        refs = cleaned["messages"][0]["images"]
        self.assertEqual(refs[0]["path"], str(self.good))
        self.assertNotIn("path", refs[1])
        self.assertNotIn("uri", refs[1])
        self.assertEqual(refs[1]["unavailable_path"], str(self.bad))
        self.assertFalse(refs[1]["media_validation"]["valid"])
        self.assertEqual(cleaned["media_validation_summary"], {"checked": 2, "unavailable": 1})
        self.assertNotIn("path", cleaned["messages"][0]["quote"]["images"][0])

    def test_qq_filename_hints_preserved_but_cached_images_checked(self):
        data = {"media": {"images": ["abcd.jpg"], "cached_images": [
            {"image": "abcd.jpg", "cache_path": str(self.bad), "source_path": str(self.good)}]}}
        output = media.sanitize_media_payload(data)
        self.assertEqual(output["media"]["images"], ["abcd.jpg"])
        ref = output["media"]["cached_images"][0]
        self.assertNotIn("cache_path", ref)
        self.assertEqual(ref["source_path"], str(self.good))

    def test_non_image_media_and_remote_urls_unchanged(self):
        data = {"videos": [{"path": "missing.mp4"}], "files": [{"path": "missing.pdf"}],
                "images": [{"path": "https://example.com/test.jpg"}]}
        self.assertEqual(media.sanitize_media_payload(data), data)

    def test_absolute_image_strings_and_resource_refs_checked(self):
        output = media.sanitize_media_payload({"images": [str(self.bad)],
            "resources": [{"path": str(self.bad)}]})
        self.assertNotIn("path", output["resources"][0])
        self.assertEqual(output["images"][0]["unavailable_path"], str(self.bad))

    def test_repair_rejects_arbitrary_files_outside_wechat_cache(self):
        dat = self.root / "bad.dat"
        dat.write_bytes(b"unused")
        self.assertEqual(media.repair_image_path(self.bad, dat)["status"], "repair_out_of_scope")
        self.assertFalse((self.root / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
