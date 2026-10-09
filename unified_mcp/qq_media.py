"""Resolve QQ image hints for a selected message page without changing files.

Exact payload filenames are tried in the message/current month first. MD5
filenames can additionally use historical Pic month folders, since migrated
files may have a different cache month. Only month directory names are listed;
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
                    hasher = hashlib.sha256()
                    with actual.open("rb") as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            hasher.update(chunk)
                    digest = hasher.hexdigest()
                    after = actual.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        last_failure = "changed_during_read"
                        continue
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
    months = list(dict.fromkeys(filter(None, (_message_month(message), current.strftime("%Y-%m")))))
    bases = [root / "Pic" / month for month in months] + [root / "Pic"]
    other_bases = [base for base in historical_bases if base not in bases]
    searched_months = list(months)
    items = []
    seen_hints = set()
    for hint in hints:
        # Filename-only lookups prevent path traversal and wildcard matches.
        if not isinstance(hint, str) or not IMAGE_NAME.fullmatch(hint) or ".." in hint:
            items.append({"status": "invalid_hint"})
            continue
        if hint.lower() in seen_hints:
            continue
        seen_hints.add(hint.lower())
        item: dict[str, Any] = {"hint": hint, "status": "not_found"}
        matched = 0
        for family in FAMILIES:
            if item["status"] == "available":
                break
            for base in bases:
                candidate = base / family / hint
                try:
                    if not candidate.is_file():
                        continue
                    actual = candidate.resolve(strict=True)
                    if not actual.is_relative_to(root):
                        item["status"] = "outside_data_root"
                        continue
                except (OSError, RuntimeError):
                    continue
                matched += 1
                decoded = _decoded_image(actual, cache)
                if decoded["status"] != "available":
                    item["status"] = decoded["status"]
                    continue
                result["images"].append({**decoded, "hint": hint, "source": "qq_local_cache",
                                         "variant": "thumbnail" if family.startswith("Thumb") else "original"})
                item["status"] = "available"
                break
        if item["status"] != "available" and HASH_IMAGE_NAME.fullmatch(hint):
            extra = _historical_image(hint, other_bases, root, cache)
            searched_months.extend(base.name for base in other_bases if base.name not in searched_months)
            matched += extra.pop("matched_files")
            if extra["status"] == "available":
                result["images"].append({**extra, "hint": hint, "source": "qq_local_cache"})
                item.update(status="available", lookup_scope=extra["lookup_scope"], cache_month=extra["cache_month"])
            elif extra["status"] == "ambiguous_cache_files":
                item["status"] = extra["status"]
            elif item["status"] == "not_found" and extra["status"] != "not_found":
                item["status"] = extra["status"]
        item["matched_files"] = matched
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
    ``images`` contains at most one successfully decoded file per unique hint.
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
