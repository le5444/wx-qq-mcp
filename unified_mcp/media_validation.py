"""Read-only image validation, output filtering, and explicit cache-copy repair.

Normal gateway reads should call ``sanitize_media_payload``. Repair is separate,
never changes source media/configuration, and refuses ambiguous JPEG candidates.
Pillow decoding establishes readability, not that the image depicts the right thing.
"""

from __future__ import annotations

import hashlib
import io
import ntpath
import os
from pathlib import Path
import struct
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname
import warnings

from unified_mcp.runtime_paths import RUNTIME

try:
    from PIL import Image, ImageFile
except ImportError:  # Keep the gateway operational, but fail closed on images.
    Image = ImageFile = None


MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_TOTAL_PIXELS = 80_000_000
MAX_FRAMES = 200
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif", ".tif", ".tiff", ".avif"}
IMAGE_CONTAINERS = {"images", "cover_images", "cached_images", "thumbnails", "image", "cover"}
PATH_KEYS = {"path", "local_path", "decoded_path", "cache_path", "source_path", "uri", "local_uri", "decoded_uri",
             "image_path", "thumbnail_path", "cover_path"}
PATH_LIST_KEYS = {"image_paths", "decoded_local_paths", "decoded_media_local_paths", "direct_readable_local_paths"}


def _failure(status: str, detail: str, **extra: Any) -> dict:
    return {"valid": False, "status": status, "warning": f"image_{status}: {detail}", **extra}


def _local_path(value: str | os.PathLike) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("empty or invalid local path")
    if raw.lower().startswith("file:"):
        parsed = urlsplit(raw)
        if parsed.netloc not in ("", "localhost"):
            raise ValueError("network file URI is not local media")
        raw = url2pathname(parsed.path)
    elif "://" in raw:
        raise ValueError("remote URL is not local media")
    if raw.startswith(("\\\\", "//")):
        raise ValueError("network paths are not local media")
    return Path(raw)


def _decode_bytes(data: bytes) -> dict:
    if Image is None:
        return _failure("decoder_unavailable", "Pillow is unavailable; readability was not verified.")
    if ImageFile.LOAD_TRUNCATED_IMAGES:
        return _failure("decoder_not_strict", "Pillow permits truncated images; strict decoding is required.")
    if not data:
        return _failure("empty", "The local image file is empty.")
    if len(data) > MAX_FILE_BYTES:
        return _failure("size_limit", "The image exceeds the validation byte limit.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # verify() alone is insufficient for JPEG: it may check only headers.
            with Image.open(io.BytesIO(data)) as probe:
                fmt = probe.format
                width, height = probe.size
                if width * height > MAX_PIXELS:
                    return _failure("pixel_limit", "The image exceeds the validation pixel limit.")
                probe.verify()
            with Image.open(io.BytesIO(data)) as decoded:
                frame_count = 0
                total_pixels = 0
                while True:
                    if frame_count >= MAX_FRAMES:
                        return _failure("frame_limit", "The animation exceeds the validation frame limit.")
                    frame_pixels = decoded.width * decoded.height
                    total_pixels += frame_pixels
                    if frame_pixels > MAX_PIXELS or total_pixels > MAX_TOTAL_PIXELS:
                        return _failure("pixel_limit", "The image exceeds the validation pixel limit.")
                    decoded.load()
                    frame_count += 1
                    try:
                        decoded.seek(frame_count)
                    except EOFError:
                        break
        return {"valid": True, "status": "readable", "format": fmt,
                "width": width, "height": height, "frames": frame_count}
    except Exception as exc:
        # Do not relay raw exception strings, which can contain paths or payloads.
        return _failure("decode_failed", "The file exists but full image decoding failed.", error_type=type(exc).__name__)


def _read_local_image(path: Path) -> tuple[bytes | None, dict | None]:
    try:
        before = path.stat()
        if not path.is_file():
            return None, _failure("not_file", "The local image path is not a regular file.")
        if before.st_size > MAX_FILE_BYTES:
            return None, _failure("size_limit", "The image exceeds the validation byte limit.")
        with path.open("rb") as stream:
            # Allocate for the actual file, while retaining an extra byte and
            # the post-read stat check to detect growth during the read.
            data = stream.read(min(before.st_size + 1, MAX_FILE_BYTES + 1))
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            return None, _failure("changed_during_read", "The image changed during validation; retry the read.")
        return data, None
    except FileNotFoundError:
        return None, _failure("missing", "The local image file does not exist.")
    except (OSError, ValueError) as exc:
        return None, _failure("read_failed", "The local image file could not be read.", error_type=type(exc).__name__)


def validate_image_path(path: str | os.PathLike) -> dict:
    """Fully decode a local image without modifying it; return a JSON-safe verdict."""
    try:
        local = _local_path(path)
    except (TypeError, ValueError, OSError):
        return _failure("invalid_path", "The value is not a supported local image path.")
    data, error = _read_local_image(local)
    return error if error is not None else _decode_bytes(data)


def _image_reference(value: Any, image_context: bool) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if "://" in value and not value.lower().startswith("file:"):
        return False
    return image_context or Path(value).suffix.lower() in IMAGE_EXTENSIONS


def sanitize_media_payload(payload: Any) -> Any:
    """Copy JSON data, retaining bad-image locations only as unavailable_* fields.

    Handles nested WeChat image refs and QQ cached_images, including file URIs.
    Bare QQ image filename hints stay intact; they never promised a local file.
    Non-image file/video refs and remote URLs are preserved. No repair is invoked.
    """
    checked: dict[str, dict] = {}

    def verdict(value: str) -> dict:
        try:
            key = str(_local_path(value))
        except (ValueError, TypeError, OSError):
            key = value
        if key not in checked:
            checked[key] = validate_image_path(value)
        return checked[key]

    def walk(value: Any, image_context: bool = False) -> Any:
        if isinstance(value, list):
            return [walk(item, image_context) for item in value]
        if isinstance(value, str) and image_context and (ntpath.isabs(value) or value.lower().startswith("file:")):
            result = verdict(value)
            if not result["valid"]:
                return {"unavailable_path": value, "media_validation": result, "warnings": [result["warning"]]}
            return value
        if not isinstance(value, dict):
            return value
        own_image = image_context or value.get("resource_family") in ("image", "cover") or value.get("kind") == "image"
        own_image |= str(value.get("mime_type", "")).startswith("image/")
        out = {key: walk(item, key in IMAGE_CONTAINERS or key in PATH_LIST_KEYS)
               for key, item in value.items()}
        failures = []
        for key in PATH_KEYS.intersection(value):
            candidate = value[key]
            if not _image_reference(candidate, own_image or key in {"image_path", "thumbnail_path", "cover_path"}):
                continue
            result = verdict(candidate)
            if not result["valid"]:
                out.pop(key, None)
                out["unavailable_" + key] = candidate
                failures.append(result)
        if failures:
            out["media_validation"] = failures[0]
            existing = out.get("warnings", [])
            if not isinstance(existing, list):
                existing = [existing]
            out["warnings"] = existing + list(dict.fromkeys(item["warning"] for item in failures if item["warning"] not in existing))
        return out

    output = walk(payload)
    failed = sum(not result["valid"] for result in checked.values())
    if failed and isinstance(output, dict):
        notice = f"image_paths_unavailable: {failed} local image file(s) failed validation; see unavailable_path/media_validation."
        previous = output.get("warnings", [])
        output["warnings"] = (previous if isinstance(previous, list) else [previous]) + [notice]
        output["media_validation_summary"] = {"checked": len(checked), "unavailable": failed}
    return output


def repair_image_path(path: str | os.PathLike, source_dat_path: str | os.PathLike,
                      output_dir: str | os.PathLike | None = None) -> dict:
    """Explicitly try XOR-tail corrections; write only a unique readable copy.

    The caller must supply the original matching WeChat V4 DAT. Input must be in
    the current user's WeChat caches/data; output is restricted to this gateway's
    WXQQ_DATA_DIR/media-repair tree. This does not read keys, refresh them, or overwrite
    any original. Multiple decodable candidates are ambiguous and are not saved.
    """
    try:
        cache_path = _local_path(path).resolve(strict=True)
        dat_path = _local_path(source_dat_path).resolve(strict=True)
        cache_roots = [(Path.home() / name / "media-cache").resolve() for name in (".wx-mcp", ".wechat-cli")]
        dat_root = (Path.home() / "xwechat_files").resolve()
        runtime_root = (RUNTIME / "media-repair").resolve()
        destination_dir = Path(output_dir).resolve() if output_dir is not None else runtime_root
        if not any(cache_path.is_relative_to(root) for root in cache_roots) or not dat_path.is_relative_to(dat_root):
            return _failure("repair_out_of_scope", "Repair input is outside the supported WeChat media directories.")
        if not destination_dir.is_relative_to(runtime_root):
            return _failure("repair_out_of_scope", "Repair output must remain in WXQQ_DATA_DIR/media-repair.")
        if dat_path.suffix.lower() != ".dat" or not cache_path.stem.startswith(dat_path.stem + "-"):
            return _failure("repair_source_mismatch", "The DAT filename does not match this decoded cache entry.")
    except (OSError, ValueError, TypeError):
        return _failure("repair_invalid_path", "Repair input paths are unavailable or invalid.")
    cached, error = _read_local_image(cache_path)
    if error is not None:
        return error
    initial = _decode_bytes(cached)
    if initial["valid"]:
        return {**initial, "path": str(cache_path), "repair_status": "already_readable"}
    if initial["status"] != "decode_failed":
        return initial
    dat, error = _read_local_image(dat_path)
    if error is not None:
        return error
    if len(dat) < 15 or dat[:6] not in (b"\x07\x08V1\x08\x07", b"\x07\x08V2\x08\x07"):
        return _failure("repair_unsupported", "The source is not a supported WeChat V4 image DAT.")
    aes_size, xor_size = struct.unpack("<II", dat[6:14])
    padding = 16 - aes_size % 16 if aes_size else 0
    if len(cached) != len(dat) - 15 - padding or not 0 < xor_size <= len(cached) - aes_size:
        return _failure("repair_source_mismatch", "The DAT segment sizes do not match the decoded cache.")
    if hashlib.sha256(cached).hexdigest()[:16] != cache_path.stem.rsplit("-", 1)[-1]:
        return _failure("repair_source_mismatch", "The cache content does not match its recorded digest.")
    old_tail, raw_tail = cached[-xor_size:], dat[-xor_size:]
    difference = old_tail[0] ^ raw_tail[0]
    if any((left ^ right) != difference for left, right in zip(old_tail, raw_tail)):
        return _failure("repair_source_mismatch", "The DAT XOR segment does not correspond to this cache entry.")
    prefix = cached[:-xor_size]
    candidates = []
    for delta in range(1, 256):
        corrected = prefix + old_tail.translate(bytes(value ^ delta for value in range(256)))
        result = _decode_bytes(corrected)
        if result["valid"]:
            candidates.append((corrected, result))
            if len(candidates) > 1:
                return _failure("repair_ambiguous", "Multiple candidates decode; no repaired file was written.")
    if not candidates:
        return _failure("repair_no_candidate", "No complete image decoded; no repaired file was written.")
    corrected, result = candidates[0]
    digest = hashlib.sha256(corrected).hexdigest()
    ext = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp", "BMP": ".bmp"}.get(result["format"])
    if ext is None:
        return _failure("repair_unsupported", "The recovered format is not supported for cache-copy repair.")
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{dat_path.stem}-{digest[:16]}{ext}"
        try:
            with destination.open("xb") as stream:
                stream.write(corrected)
        except FileExistsError:
            if destination.read_bytes() != corrected:
                return _failure("repair_output_collision", "The proposed output exists with different bytes.")
        return {**result, "path": str(destination), "repair_status": "recovered_copy",
                "sha256": digest, "source_unchanged": True}
    except OSError as exc:
        return _failure("repair_write_failed", "The recovered copy could not be saved.", error_type=type(exc).__name__)
