import unittest
import hashlib
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from PIL import Image

from unified_mcp.image_text import (ImageTextReader, image_reference_rank, normalize_ocr_text,
                                   needs_large_image_ocr_upgrade, tile_boxes, stitch_tile_text)


class ImageTextTests(unittest.TestCase):
    def test_nested_dimensions_select_original_even_when_thumbnail_first(self):
        small = {'path': 'small.jpg', 'media_validation': {'width': 90, 'height': 200}, 'variant': 'thumbnail'}
        large = {'path': 'large.jpg', 'media_validation': {'width': 1080, 'height': 2400}, 'variant': 'high'}
        self.assertIs(max([small, large], key=image_reference_rank), large)
        absent = {'unavailable_path': 'huge.jpg', 'media_validation': {'width': 5000, 'height': 5000}}
        self.assertIs(max([absent, small], key=image_reference_rank), small)

    def test_chinese_spacing_retains_raw_evidence_and_line_boundaries(self):
        raw = '去 向 登 记\nEnglish words 123\n中 文'
        result = normalize_ocr_text({'text': raw})
        self.assertEqual(result['raw_text'], raw)
        self.assertEqual(result['text'], '去向登记\nEnglish words 123\n中文')

    def test_tiles_cover_original_pixels_with_exact_overlap_and_no_empty_tail(self):
        self.assertEqual(tile_boxes(3500, 3500), [(0, 0, 3500, 3500)])
        boxes = tile_boxes(100, 6920)
        self.assertEqual(boxes, [(0, 0, 100, 3500), (0, 3420, 100, 6920)])
        boxes = tile_boxes(7000, 7000)
        self.assertEqual(len(boxes), 9)
        self.assertEqual(boxes[-1], (6840, 6840, 7000, 7000))
        for left, top, right, bottom in boxes:
            self.assertTrue(0 < right-left <= 3500 and 0 < bottom-top <= 3500)
        for bad in ((0, 1, 3500, 80), (1, 1, 80, 80), (1, 1, 3500, -1)):
            with self.assertRaises(ValueError):
                tile_boxes(*bad)

    def test_only_adjacent_vertical_identical_line_overlap_is_collapsed(self):
        first = {'box': [0, 0, 100, 3500], 'raw_text': '首 行\n重复行\n第二行'}
        second = {'box': [0, 3420, 100, 6920], 'raw_text': '重复行\n第二行\n尾 行'}
        raw, removed = stitch_tile_text([first, second])
        self.assertEqual(raw, '首 行\n重复行\n第二行\n尾 行')
        self.assertEqual(removed, 2)
        self.assertEqual(second['raw_text'], '重复行\n第二行\n尾 行')
        horizontal = {**second, 'box': [3420, 0, 6920, 3500]}
        self.assertEqual(stitch_tile_text([first, horizontal])[1], 0)
        separated = {**second, 'box': [0, 3500, 100, 7000]}
        self.assertEqual(stitch_tile_text([first, separated])[1], 0)
        differing = {**second, 'raw_text': '重复 行\n第二行\n尾 行'}
        self.assertEqual(stitch_tile_text([first, differing])[1], 0)

    def test_old_large_ocr_requires_upgrade_but_old_small_cache_does_not(self):
        old = {'status': 'ok', 'text': 'old'}
        self.assertFalse(needs_large_image_ocr_upgrade(old, 4000, 2000))
        self.assertTrue(needs_large_image_ocr_upgrade(old, 4001, 2000))
        fresh = {'status': 'ok', 'preprocessing': {
            'version': 2, 'method': 'native_resolution_tiles', 'source_width': 4001, 'source_height': 2000,
            'tile_count': 2, 'completed_tiles': 2, 'complete': True}}
        self.assertFalse(needs_large_image_ocr_upgrade(fresh, 4001, 2000))
        self.assertTrue(needs_large_image_ocr_upgrade({**fresh, 'status': 'partial'}, 4001, 2000))
        self.assertTrue(needs_large_image_ocr_upgrade(fresh, 4001, 2001))


class ImageTextTilingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        self.cache = self.root / 'cache'
        self.patch = patch('unified_mcp.image_text.IMAGE_TEXT_RUNTIME', self.runtime)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.platform = patch('unified_mcp.image_text.WINDOWS_OCR_SUPPORTED', True)
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.source = self.root / 'long.png'
        Image.new('RGB', (100, 5000), 'white').save(self.source)
        self.reader = ImageTextReader(cache_dir=self.cache)
        self.addCleanup(self.reader.close)

    def cache_path(self):
        return self.cache / (hashlib.sha256(self.source.read_bytes()).hexdigest() + '.json')

    def test_native_tiles_preserve_source_clean_exact_temp_files_and_upgrade_cache(self):
        self.cache.mkdir()
        self.cache_path().write_text(json.dumps({'status': 'ok', 'text': 'old scaled result'}))
        original = self.source.read_bytes()
        stamp = self.source.stat().st_mtime_ns
        self.runtime.mkdir()
        sentinel = self.runtime / 'unrelated.png'
        sentinel.write_bytes(b'preserve')
        sizes = []
        def recognize(path):
            with Image.open(path) as image:
                sizes.append(image.size)
            return {'status': 'ok', 'text': '首 行\n接缝' if len(sizes) == 1 else '接缝\n尾 行'}
        with patch.object(self.reader, '_recognize', side_effect=recognize) as recognize_mock:
            result = self.reader.read(self.source)
            cached = self.reader.read(self.source)
        self.assertEqual(sizes, [(100, 3500), (100, 1580)])
        self.assertEqual(recognize_mock.call_count, 2)
        self.assertEqual(result['raw_text'], '首 行\n接缝\n尾 行')
        self.assertEqual(result['text'], '首行\n接缝\n尾行')
        self.assertEqual(result['tiles'][1]['raw_text'], '接缝\n尾 行')
        self.assertEqual(result['preprocessing']['overlap_lines_removed'], 1)
        self.assertTrue(cached['cached'])
        self.assertEqual(list(self.runtime.iterdir()), [sentinel])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.source.stat().st_mtime_ns, stamp)

    def test_old_small_cache_is_reused_without_worker_or_crops(self):
        Image.new('RGB', (100, 4000), 'white').save(self.source)
        self.cache.mkdir()
        self.cache_path().write_text(json.dumps({'status': 'ok', 'text': '旧 缓 存'}))
        with patch.object(self.reader, '_recognize') as recognize:
            result = self.reader.read(self.source)
        recognize.assert_not_called()
        self.assertTrue(result['cached'])
        self.assertEqual(result['raw_text'], '旧 缓 存')
        self.assertFalse(self.runtime.exists())

    def test_tile_failure_preserves_partial_text_and_never_writes_success_cache(self):
        with patch.object(self.reader, '_recognize', side_effect=[{'status': 'ok', 'text': 'first'},
                                                                  {'status': 'unavailable', 'reason': 'sample failure'}]):
            result = self.reader.read(self.source)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['raw_text'], 'first')
        self.assertFalse(result['preprocessing']['complete'])
        self.assertEqual(result['preprocessing']['completed_tiles'], 1)
        self.assertFalse(self.cache_path().exists())
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_same_worker_process_is_used_for_all_tiles(self):
        output = ''.join(json.dumps({'status': 'ok', 'text': value}) + '\n' for value in ('first', 'second'))
        process = Mock()
        process.poll.return_value = None
        process.stdin = io.StringIO()
        process.stdout = io.StringIO(output)
        with patch('unified_mcp.image_text.subprocess.Popen', return_value=process) as popen:
            result = self.reader.read(self.source)
            calls = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(not Path(call['path']).exists() for call in calls))
