"""Local Windows OCR with a shared worker and content-addressed success cache."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import threading
import uuid

from .media_validation import validate_image_path

BASE = Path(__file__).resolve().parent
from unified_mcp.runtime_paths import RUNTIME

IMAGE_TEXT_RUNTIME = RUNTIME / 'image-text'
WINDOWS_OCR_SUPPORTED = os.name == 'nt'
TILING_THRESHOLD = 4000
TILE_EDGE = 3500
TILE_OVERLAP = 80
OCR_PREPROCESSING_VERSION = 2


def needs_large_image_ocr_upgrade(result, width, height):
    """Old <=4000 native OCR remains valid; old downscaled large OCR does not."""
    if max(int(width), int(height)) <= TILING_THRESHOLD:
        return False
    preprocessing = result.get('preprocessing') if isinstance(result, dict) else None
    return not (isinstance(result, dict) and result.get('status') in ('ok', 'no_text')
                and isinstance(preprocessing, dict)
                and preprocessing.get('version') == OCR_PREPROCESSING_VERSION
                and preprocessing.get('method') == 'native_resolution_tiles'
                and preprocessing.get('source_width') == int(width)
                and preprocessing.get('source_height') == int(height)
                and preprocessing.get('complete') is True
                and preprocessing.get('completed_tiles') == preprocessing.get('tile_count'))


def tile_boxes(width, height, edge=TILE_EDGE, overlap=TILE_OVERLAP):
    """Cover the whole source with bounded native pixels, in row-major order."""
    if width <= 0 or height <= 0 or edge <= 0 or not 0 <= overlap < edge:
        raise ValueError('Invalid image dimensions or tile overlap')
    def starts(length):
        positions = [0]
        while positions[-1] + edge < length:
            positions.append(positions[-1] + edge - overlap)
        return positions
    return [(left, top, min(width, left + edge), min(height, top + edge))
            for top in starts(height) for left in starts(width)]


def stitch_tile_text(tiles):
    """Merge vertical neighbors conservatively; each tile retains its raw text.

    Windows worker lines have no coordinates. Only exact whole-line suffix /
    prefix matches at a vertical overlap are collapsed, with at most ten lines
    per seam. Horizontal tile text stays in explicit row-major block order.
    """
    merged = []
    previous = None
    removed = 0
    for tile in tiles:
        lines = str(tile.get('raw_text', '')).splitlines()
        overlap = 0
        box = tile['box']
        if previous is not None:
            prior = previous['box']
            vertical_neighbor = box[0] == prior[0] and box[2] == prior[2] and prior[1] < box[1] < prior[3]
            if vertical_neighbor:
                previous_lines = str(previous.get('raw_text', '')).splitlines()
                for count in range(min(10, len(previous_lines), len(lines)), 0, -1):
                    left = [line.strip() for line in previous_lines[-count:]]
                    right = [line.strip() for line in lines[:count]]
                    if any(left) and left == right:
                        overlap = count
                        break
        merged.extend(lines[overlap:])
        removed += overlap
        previous = tile
    return '\n'.join(merged), removed


def image_reference_rank(reference):
    if not isinstance(reference, dict):
        return (False, 0, False)
    validation = reference.get('media_validation', {})
    try:
        pixels = int(reference.get('width') or validation.get('width') or 0) * int(reference.get('height') or validation.get('height') or 0)
    except (TypeError, ValueError):
        pixels = 0
    return (bool(reference.get('path') or reference.get('cache_path')), pixels,
            reference.get('variant') not in ('thumbnail', 'thumb'))


def normalize_ocr_text(result):
    """Windows Chinese OCR separates characters; retain raw text for inspection."""
    result = dict(result)
    raw = result.get('raw_text', result.get('text', ''))
    result['raw_text'] = raw
    result['text'] = re.sub(r'(?<=[\u3400-\u9fff])[ \t]+(?=[\u3400-\u9fff])', '', raw)
    return result


class ImageTextReader:
    def __init__(self, cache_dir=None, timeout_seconds=50):
        self.cache_dir = Path(cache_dir or IMAGE_TEXT_RUNTIME)
        self.timeout_seconds = timeout_seconds
        self.process = None
        self.lock = threading.Lock()
        self.reader = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-ocr-output')

    def _stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=5)
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
            self.process = None

    def close(self):
        with self.lock:
            self._stop()
            self.reader.shutdown(wait=True, cancel_futures=True)

    def read(self, path):
        with self.lock:
            return self._read(path)

    def _read(self, path):
        path = Path(path).expanduser().resolve()
        validation = validate_image_path(path)
        if not validation['valid']:
            return {'status': 'unavailable', 'engine': 'Windows.Media.Ocr', 'reason': validation['status']}
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        cache = self.cache_dir / (digest + '.json')
        if cache.exists():
            try:
                result = json.loads(cache.read_text(encoding='utf-8'))
                if result.get('status') in ('ok', 'no_text') and not needs_large_image_ocr_upgrade(result, validation['width'], validation['height']):
                    return {**normalize_ocr_text(result), 'cached': True}
            except (OSError, ValueError):
                pass
        if not WINDOWS_OCR_SUPPORTED:
            return {'status': 'unavailable', 'reason': 'Windows OCR requires Windows'}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if max(validation['width'], validation['height']) > TILING_THRESHOLD:
            result = self._read_tiled(path, digest, validation)
        else:
            result = self._recognize(path)
            result['preprocessing'] = {'version': OCR_PREPROCESSING_VERSION, 'method': 'native_resolution',
                                       'source_width': validation['width'], 'source_height': validation['height']}
        result = normalize_ocr_text(result)
        result.update(content_sha256=digest, automated=True,
                      note='Local OCR text only; no_text does not mean the image has no other meaningful content.')
        if validation.get('frames', 1) > 1:
            result['frame_scope'] = 'first_frame_only'
        if result.get('status') in ('ok', 'no_text'):
            temporary = self.cache_dir / (digest + '-' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                temporary.replace(cache)
            finally:
                temporary.unlink(missing_ok=True)
        return result

    def _read_tiled(self, path, digest, validation):
        from PIL import Image
        boxes = tile_boxes(validation['width'], validation['height'])
        temporary_root = IMAGE_TEXT_RUNTIME.resolve()
        temporary_root.mkdir(parents=True, exist_ok=True)
        tiles = []
        with Image.open(path) as image:
            image.seek(0)
            for index, box in enumerate(boxes):
                # Every crop is an explicitly named runtime leaf, and each is
                # removed before the next tile; never enumerate for deletion.
                temporary = temporary_root / (digest[:16] + '-tile-' + uuid.uuid4().hex + '.png')
                try:
                    with image.crop(box).convert('RGB') as crop:
                        crop.save(temporary)
                    result = self._recognize(temporary)
                finally:
                    temporary.unlink(missing_ok=True)
                tiles.append({'index': index, 'box': list(box), 'status': result.get('status', 'unavailable'),
                              'raw_text': result.get('raw_text', result.get('text', ''))})
                if result.get('status') not in ('ok', 'no_text'):
                    tiles[-1]['reason'] = result.get('reason') or result.get('error') or 'tile_ocr_failed'
                    break
        raw, removed = stitch_tile_text(tiles)
        complete = len(tiles) == len(boxes) and all(tile['status'] in ('ok', 'no_text') for tile in tiles)
        status = ('ok' if raw.strip() else 'no_text') if complete else 'partial' if raw.strip() else 'unavailable'
        return {'status': status, 'engine': 'Windows.Media.Ocr', 'language': result.get('language'),
                'text': raw, 'raw_text': raw, 'lines': raw.splitlines(), 'tiles': tiles,
                'preprocessing': {'version': OCR_PREPROCESSING_VERSION, 'method': 'native_resolution_tiles',
                                  'source_width': validation['width'], 'source_height': validation['height'],
                                  'tile_edge': TILE_EDGE, 'overlap_px': TILE_OVERLAP, 'tile_count': len(boxes),
                                  'completed_tiles': sum(tile['status'] in ('ok', 'no_text') for tile in tiles),
                                  'complete': complete,
                                  'overlap_lines_removed': removed, 'order': 'row_major'},
                'tiling_note': 'Native-resolution crops; exact repeated lines at vertical seams may be merged. '
                               'Each tile preserves raw engine text. Multi-column tile order is approximate.'}

    def _recognize(self, ocr_path):
        """Send each source/crop to the same persistent PowerShell worker."""
        try:
            if self.process is None or self.process.poll() is not None:
                self._stop()
                self.process = subprocess.Popen(
                    ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                     '-File', str(BASE / 'windows_ocr_worker.ps1')],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding='utf-8', bufsize=1, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.process.stdin.write(json.dumps({'path': str(ocr_path)}, ensure_ascii=False) + '\n')
            self.process.stdin.flush()
            line = self.reader.submit(self.process.stdout.readline).result(timeout=self.timeout_seconds)
            result = json.loads(line)
            if not isinstance(result, dict) or result.get('status') not in ('ok', 'no_text', 'unavailable'):
                raise ValueError('Unexpected Windows OCR result')
        except (OSError, ValueError, FutureTimeout):
            self._stop()
            return {'status': 'unavailable', 'engine': 'Windows.Media.Ocr', 'reason': 'worker_failed_or_timed_out'}
        return result
