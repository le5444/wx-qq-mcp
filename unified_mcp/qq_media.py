"""Resolve QQ image hints for a selected message page without changing files.

Ordinary payload filenames are limited to the message month. Hash-shaped
filenames may use current or historical Pic folders, but conflicting bytes in
one variant are rejected before selecting any month. A thumbnail is not required
to have the original image's byte digest. Only month directory names are listed;
there is no recursive scan, download, wildcard filename match, or source write.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import warnings
from pathlib import Path
from typing import Any


LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
IMAGE_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,199}\.(?:jpg|jpeg|png|gif|webp)", re.I)
HASH_IMAGE_NAME = re.compile(r"[a-f0-9]{32}\.(?:jpg|jpeg|png|gif|webp)", re.I)
MONTH_NAME = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")
FAMILIES = ("Ori", "OriTemp", "Thumb", "ThumbTemp")


def _message_month(message: dict[str, Any]) -> str | None:
    for key in ("timestamp", "create_time", "time"):
        value = message.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            if isinstance(value, (int, float)) or re.fullmatch(r"\d+(?:\.\d+)?", str(value)):
                timestamp = float(value)
                if timestamp >= 100_000_000_000:
                    timestamp /= 1000
                date = dt.datetime.fromtimestamp(timestamp, LOCAL_TZ)
            else:
                date = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                date = date.replace(tzinfo=LOCAL_TZ) if date.tzinfo is None else date.astimezone(LOCAL_TZ)
            return date.strftime("%Y-%m")
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    return None


def _decoded_image(path: Path, cache: dict) -> dict[str, Any]:
    try:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return {"status": "unreadable"}
    if key in cache:
        return dict(cache[key])
    try:
        from PIL import Image
    except ImportError:
        return {"status": "decoder_unavailable"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                image_format = image.format
                frames = getattr(image, "n_frames", 1)
                for frame in range(frames):
                    image.seek(frame)
                    image.load()
        # Files still being downloaded must not be presented as validated.
        after = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            result = {"status": "changed_during_read"}
        else:
            result = {"status": "available", "path": str(path), "bytes": stat.st_size,
                      "format": image_format, "width": width, "height": height,
                      "frame_count": frames}
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        result = {"status": "unreadable"}
    cache[key] = result
    return dict(result)


def _historical_bases(root: Path) -> list[Path]:
    """List direct month directories only, retaining the configured data root."""
    pic = root / "Pic"
    try:
        if not pic.resolve().is_relative_to(root):
            return []
        return sorted((p for p in pic.iterdir() if MONTH_NAME.fullmatch(p.name)
                       and p.is_dir() and p.resolve().is_relative_to(root)), reverse=True)
    except (OSError, RuntimeError):
        return []


def _historical_image(hint: str, bases: list[Path], root: Path, cache: dict) -> dict[str, Any]:
    """Resolve a hash filename; reject conflicting bytes within one variant."""
    matched = 0
    last_failure = None
    for family in FAMILIES:
        available = []
        for base in bases:
            candidate = base / family / hint
            try:
                if not candidate.is_file():
                    continue
                actual = candidate.resolve(strict=True)
                if not actual.is_relative_to(root):
                    continue
                matched += 1
                before = actual.stat()
                decoded = _decoded_image(actual, cache)
                if decoded["status"] == "available":
                    hash_key = ('sha256', str(actual), before.st_size, before.st_mtime_ns)
                    digest = cache.get(hash_key)
                    if digest is None:
                        hasher = hashlib.sha256()
                        with actual.open("rb") as source:
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                hasher.update(chunk)
                        digest = hasher.hexdigest()
                    after = actual.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        last_failure = "changed_during_read"
                        continue
                    cache[hash_key] = digest
                    available.append({**decoded, "sha256": digest, "cache_month": base.name})
                else:
                    last_failure = decoded["status"]
            except (OSError, RuntimeError):
                continue
        if available:
            if len({item["sha256"] for item in available}) != 1:
                return {"status": "ambiguous_cache_files", "matched_files": matched}
            return {**available[0], "matched_files": matched,
                    "variant": "thumbnail" if family.startswith("Thumb") else "original",
                    "lookup_scope": "historical_month_exact_hash"}
    return {"status": last_failure or "not_found", "matched_files": matched}


def _resolve(message: dict[str, Any], root: Path, current: dt.datetime, cache: dict,
             historical_bases: list[Path]) -> dict[str, Any]:
    result = dict(message)
    result["images"] = []
    media = message.get("media")
    hints = media.get("images", []) if isinstance(media, dict) else []
    if isinstance(hints, str):
        hints = [hints]
    if not isinstance(hints, (list, tuple)):
        hints = []
    message_month = _message_month(message)
    months = list(dict.fromkeys(filter(None, (message_month, current.strftime("%Y-%m")))))
    primary_bases = [root / "Pic" / month for month in months] + [root / "Pic"]
    all_bases = list(dict.fromkeys([*primary_bases, *historical_bases]))
    searched_months = []
    items = []
    resolved_hints = {}
    for occurrence, hint in enumerate(hints):
        if not isinstance(hint, str) or not IMAGE_NAME.fullmatch(hint) or ".." in hint:
            items.append({"status": "invalid_hint", "occurrence": occurrence})
            continue
        hashed = bool(HASH_IMAGE_NAME.fullmatch(hint))
        # An ordinary basename is not a cross-month identity. Keep it strictly
        # within the message month. Hash-shaped names may have migrated, but
        # every matching file in the same variant must agree on its bytes.
        bases = all_bases if hashed else [root / "Pic" / message_month] if message_month else []
        for base in bases:
            if MONTH_NAME.fullmatch(base.name) and base.name not in searched_months:
                searched_months.append(base.name)
        cache_key = (hint.lower(), tuple(str(base) for base in bases))
        if cache_key not in resolved_hints:
            resolved_hints[cache_key] = _historical_image(hint, bases, root, cache)
        found = dict(resolved_hints[cache_key])
        item = {"hint": hint, "status": found['status'], "occurrence": occurrence,
                "matched_files": found.get('matched_files', 0)}
        if found['status'] == 'available':
            candidate = Path(found['path'])
            origin = candidate.parent.parent
            scope = 'message_month_exact_filename' if message_month and origin.name == message_month else (
                'current_or_common_exact_hash' if origin in primary_bases else 'historical_month_exact_hash')
            reference = {**found, "hint": hint, "source": "qq_local_cache", "lookup_scope": scope,
                         "media_occurrence": occurrence,
                         "identity_evidence": "exact_filename_consistent_within_variant",
                         "content_hash_matches_filename_verified": False}
            result['images'].append(reference)
            item.update(lookup_scope=scope, cache_month=found.get('cache_month'))
        items.append(item)
    available = len(result["images"])
    status = "no_image_hints" if not items else "ok" if available == len(items) else "partial" if available else "unavailable"
    result["qq_media_resolution"] = {"status": status, "items": items, "searched_months": searched_months}
    return result


def resolve_page_images(messages: list[dict[str, Any]], data_root: str | Path,
                        *, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Return copied messages with verified ``images`` and resolution statuses.

    Call this only after filtering/pagination. Original ``media.images`` hints
    remain unchanged. A per-call cache avoids decoding a shared image twice.
    Repeated hints retain their occurrence order; file validation is cached.
    """
    root = Path(data_root).expanduser().resolve()
    current = now or dt.datetime.now(LOCAL_TZ)
    current = current.replace(tzinfo=LOCAL_TZ) if current.tzinfo is None else current.astimezone(LOCAL_TZ)
    cache: dict = {}
    historical_bases = _historical_bases(root)
    return [_resolve(message, root, current, cache, historical_bases) for message in messages]


def resolve_message_images(message: dict[str, Any], data_root: str | Path,
                           *, now: dt.datetime | None = None) -> dict[str, Any]:
    """Resolve one record; prefer ``resolve_page_images`` for a result page."""
    return resolve_page_images([message], data_root, now=now)[0]
