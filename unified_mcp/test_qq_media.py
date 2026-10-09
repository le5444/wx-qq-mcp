import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from unified_mcp.qq_media import resolve_message_images, resolve_page_images


class QQMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = dt.datetime(2026, 10, 9, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.name = "0123456789abcdef0123456789abcdef.png"

    def make_image(self, relative, *, frames=1):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (3, 2), "blue")
        if frames > 1:
            image.save(path, save_all=True, append_images=[Image.new("RGB", (3, 2), "red")])
        else:
            image.save(path)
        return path

    def record(self, *names):
        return {"msg_id": "selected", "time": "2024-03-12 09:10:11+08:00",
                "media": {"images": list(names or [self.name])}}

    def test_historical_month_exact_match_and_original_untouched(self):
        path = self.make_image(f"Pic/2024-03/Ori/{self.name}")
        original = self.record()
        saved = copy.deepcopy(original)
        file_bytes = path.read_bytes()
        file_stat = path.stat()
        result = resolve_message_images(original, self.root, now=self.now)
        self.assertEqual(result["images"][0]["path"], str(path.resolve()))
        self.assertEqual(result["images"][0]["format"], "PNG")
        self.assertEqual(result["qq_media_resolution"]["status"], "ok")
        self.assertEqual(original, saved)
        self.assertEqual(path.read_bytes(), file_bytes)
        self.assertEqual(path.stat().st_mtime_ns, file_stat.st_mtime_ns)

    def test_truncated_file_is_not_exposed_as_readable(self):
        path = self.make_image(f"Pic/2024-03/Ori/{self.name}")
        path.write_bytes(path.read_bytes()[:45])
        result = resolve_message_images(self.record(), self.root, now=self.now)
        self.assertEqual(result["images"], [])
        self.assertEqual(result["qq_media_resolution"]["items"][0]["status"], "unreadable")
        self.assertNotIn("path", result["qq_media_resolution"]["items"][0])

    def test_current_month_and_top_level_fallback(self):
        for folder in ("Pic/2026-10/OriTemp", "Pic/Thumb"):
            with self.subTest(folder=folder):
                path = self.make_image(f"{folder}/{self.name}")
                result = resolve_message_images(self.record(), self.root, now=self.now)
                self.assertEqual(result["images"][0]["path"], str(path.resolve()))
                path.unlink()

    def test_corrupt_original_can_fall_back_to_decodable_thumbnail(self):
        corrupt = self.root / "Pic/2024-03/Ori" / self.name
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"not an image")
        thumbnail = self.make_image(f"Pic/2024-03/Thumb/{self.name}")
        result = resolve_message_images(self.record(), self.root, now=self.now)
        self.assertEqual(result["images"][0]["path"], str(thumbnail.resolve()))
        self.assertEqual(result["images"][0]["variant"], "thumbnail")

    def test_historical_hash_name_resolves_and_path_hints_are_rejected(self):
        historical = self.make_image(f"Pic/2023-01/Ori/{self.name}")
        self.make_image("Pic/2024-03/Ori/unrelated.png")
        result = resolve_message_images(self.record(self.name, "../unrelated.png", "*.png"), self.root, now=self.now)
        self.assertEqual(result["images"][0]["path"], str(historical.resolve()))
        self.assertEqual(result["images"][0]["lookup_scope"], "historical_month_exact_hash")
        self.assertEqual([item["status"] for item in result["qq_media_resolution"]["items"]],
                         ["available", "invalid_hint", "invalid_hint"])

    def test_common_basename_does_not_match_another_month(self):
        self.make_image("Pic/2023-01/Ori/photo.png")
        result = resolve_message_images(self.record("photo.png"), self.root, now=self.now)
        self.assertEqual(result["images"], [])
        self.assertEqual(result["qq_media_resolution"]["items"][0]["status"], "not_found")

    def test_corrupt_historical_file_is_reported_as_unreadable(self):
        path = self.make_image(f"Pic/2023-01/Ori/{self.name}")
        path.write_bytes(path.read_bytes()[:45])
        result = resolve_message_images(self.record(), self.root, now=self.now)
        self.assertEqual(result["images"], [])
        self.assertEqual(result["qq_media_resolution"]["items"][0]["status"], "unreadable")

    def test_conflicting_historical_hash_files_are_ambiguous(self):
        self.make_image(f"Pic/2023-01/Ori/{self.name}")
        alternate = self.make_image(f"Pic/2023-02/Ori/{self.name}")
        Image.new("RGB", (4, 3), "red").save(alternate)
        result = resolve_message_images(self.record(), self.root, now=self.now)
        self.assertEqual(result["images"], [])
        self.assertEqual(result["qq_media_resolution"]["items"][0]["status"], "ambiguous_cache_files")

    def test_matching_historical_copies_are_safe_and_non_month_folders_are_excluded(self):
        self.make_image(f"Pic/2023-01/Ori/{self.name}")
        self.make_image(f"Pic/2023-02/Ori/{self.name}")
        result = resolve_message_images(self.record(), self.root, now=self.now)
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["qq_media_resolution"]["items"][0]["matched_files"], 2)
        name = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"
        self.make_image(f"Pic/not-a-month/Ori/{name}")
        self.make_image(f"Pic/2023-99/Ori/{name}")
        self.assertEqual(resolve_message_images(self.record(name), self.root, now=self.now)["images"], [])

    def test_selected_page_animation_and_partial_results(self):
        name = "0123456789abcdef.gif"
        self.make_image(f"Pic/2024-03/Ori/{name}", frames=2)
        page = resolve_page_images([self.record(name, "missing.png"), {"msg_id": "text"}], self.root, now=self.now)
        self.assertEqual(len(page), 2)
        self.assertEqual(page[0]["images"][0]["frame_count"], 2)
        self.assertEqual(page[0]["qq_media_resolution"]["status"], "partial")
        self.assertEqual(page[1]["qq_media_resolution"]["status"], "no_image_hints")


if __name__ == "__main__":
    unittest.main()
