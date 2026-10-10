"""Exact local WeChat video resolution, bounded stream probes and preview frames.

This module never downloads, uploads, transcodes whole videos, or changes source
files. A successful probe is evidence of a local video stream, not a claim that
the entire video was watched or every frame decoded.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import xml.etree.ElementTree as ET

from unified_mcp.media_validation import validate_image_path
from unified_mcp.wechat_media import _identity
from unified_mcp.message_identity import IdentityError
from unified_mcp.runtime_paths import RUNTIME

HEX = re.compile(r"^[0-9a-fA-F]{32}$")
TZ = dt.timezone(dt.timedelta(hours=8))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _binary(name):
    explicit = os.environ.get("UNIFIED_" + name.upper())
    if explicit and Path(explicit).is_file():
        return str(Path(explicit).resolve())
    found = shutil.which(name)
    if found:
        return found
    sibling = shutil.which("ffmpeg")
    if sibling:
        candidate = Path(sibling).with_name(name + (".exe" if os.name == "nt" else ""))
        if candidate.is_file():
            return str(candidate)
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            pass
    return None


def _run(command, timeout=30):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=NO_WINDOW, check=False)
        return result, None
    except subprocess.TimeoutExpired:
        return None, "probe_timeout"
    except OSError:
        return None, "probe_launch_failed"


def _ffmpeg_info(stderr):
    duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    video = re.search(r"Stream\s+#0:\d+(?:\[[^\]]+\])?(?:\([^)]*\))?:\s*Video:\s*([^,\s]+)([^\n]*)", stderr)
    if not duration or not video:
        return None
    size = re.search(r"\b(\d{2,5})x(\d{2,5})\b", video[2])
    if not size:
        return None
    seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
    if seconds <= 0:
        return None
    return {"codec": video[1], "width": int(size[1]), "height": int(size[2]), "duration_seconds": seconds}


def probe_video(path, ffprobe=None, ffmpeg=None, timeout=30):
    """Probe metadata and decode one frame. Source contents are unchanged."""
    path = Path(path)
    try:
        before = path.stat()
    except OSError:
        return {"valid": False, "status": "source_missing"}
    if not path.is_file() or before.st_size <= 0:
        return {"valid": False, "status": "empty_or_not_file"}
    ffprobe = ffprobe or _binary("ffprobe")
    ffmpeg = ffmpeg or _binary("ffmpeg")
    metadata = None
    if ffprobe:
        cmd = [ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "v:0",
               "-show_entries", "stream=codec_name,width,height,duration:format=duration", "-of", "json", str(path)]
        result, error = _run(cmd, timeout)
        if error:
            return {"valid": False, "status": error, "probe_method": "ffprobe"}
        try:
            payload = json.loads(result.stdout)
            stream = payload.get("streams", [])[0]
            seconds = 0.0
            for candidate in (stream.get("duration"), payload.get("format", {}).get("duration")):
                try:
                    seconds = float(candidate)
                except (ValueError, TypeError):
                    continue
                if seconds > 0:
                    break
            metadata = {"codec": stream["codec_name"], "width": int(stream["width"]),
                        "height": int(stream["height"]), "duration_seconds": seconds}
            if result.returncode or min(seconds, metadata["width"], metadata["height"]) <= 0:
                metadata = None
        except (ValueError, IndexError, KeyError, TypeError):
            metadata = None
        if not metadata:
            return {"valid": False, "status": "video_stream_unavailable", "probe_method": "ffprobe"}
    if not ffmpeg:
        return {"valid": False, "status": "frame_decoder_unavailable", "metadata": metadata}
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-v", "info", "-protocol_whitelist", "file,pipe",
           "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-an", "-f", "null", "-"]
    result, error = _run(cmd, timeout)
    method = "ffprobe_and_first_frame" if ffprobe else "ffmpeg_first_frame"
    if error:
        return {"valid": False, "status": error, "probe_method": method}
    metadata = metadata or _ffmpeg_info(result.stderr)
    # Output frame counters establish that the decoder produced pixels.
    decoded = bool(re.search(r"frame=\s*[1-9]\d*", result.stderr))
    if result.returncode or not metadata or not decoded:
        return {"valid": False, "status": "video_decode_failed", "probe_method": method}
    try:
        after = path.stat()
    except OSError:
        return {"valid": False, "status": "source_changed_during_probe"}
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        return {"valid": False, "status": "source_changed_during_probe"}
    return {"valid": True, "status": "playable_local", "probe_method": method, **metadata,
            "bytes": before.st_size, "first_frame_decoded": True, "whole_video_decoded": False,
            "source_unchanged": True}


def preview_frames(path, probe, output_dir=None, ffmpeg=None):
    """Generate up to three bounded JPEG frames without touching the video."""
    if not probe.get("valid"):
        return {"status": "video_unavailable", "frames": []}
    ffmpeg = ffmpeg or _binary("ffmpeg")
    if not ffmpeg:
        return {"status": "frame_decoder_unavailable", "frames": []}
    path = Path(path)
    destination = Path(output_dir or RUNTIME / "video-previews").resolve()
    if not destination.is_relative_to(RUNTIME.resolve()):
        raise ValueError("Video preview output must remain within WXQQ_DATA_DIR")
    stat = path.stat()
    fingerprint = hashlib.sha256((str(path.resolve()) + ":" + str(stat.st_size) + ":" + str(stat.st_mtime_ns)).encode()).hexdigest()[:24]
    destination = destination / fingerprint
    destination.mkdir(parents=True, exist_ok=True)
    duration = float(probe["duration_seconds"])
    points = [("first", 0.0), ("middle", duration / 2), ("last", max(0.0, duration - .25))]
    frames = []
    errors = []
    for label, timestamp in points:
        out = destination / (label + ".jpg")
        if not out.exists():
            cmd = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-n", "-protocol_whitelist", "file,pipe",
                   "-ss", f"{timestamp:.3f}", "-i", str(path), "-map", "0:v:0", "-frames:v", "1",
                   "-vf", "scale='min(1280,iw)':-2", "-an", "-q:v", "3", str(out)]
            result, error = _run(cmd, 30)
            if error or result.returncode:
                errors.append({"position": label, "status": error or "frame_decode_failed"})
                continue
        verdict = validate_image_path(out)
        if verdict["valid"]:
            frames.append({"position": label, "time_seconds": round(timestamp, 3), "path": str(out),
                           "uri": out.as_uri(), "media_validation": verdict})
        else:
            errors.append({"position": label, "status": verdict["status"]})
    return {"status": "ready" if len(frames) == 3 else "partial" if frames else "unavailable",
            "frames": frames, "errors": errors, "whole_video_watched": False}


class WeChatVideoResolver:
    def __init__(self, account_root=None, metadata_dir=None, config_path=None):
        config_path = Path(config_path or os.environ.get("WECHAT_CLI_CONFIG") or os.environ.get("WX_MCP_CONFIG") or Path.home() / ".config/wxcli/config.json")
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            cfg = {}
        selected = account_root or os.environ.get("WECHAT_CLI_DB_ROOT") or os.environ.get("WX_MCP_DB_ROOT") or cfg.get("db_root")
        self.account = Path(selected).resolve() if selected else None
        self.metadata_dir = Path(metadata_dir or os.environ.get("UNIFIED_MEDIA_METADATA_DIR") or RUNTIME / "media_metadata")
        self.metadata = {}
        self.resources = {}
        self._probes = {}
        self._lock = threading.RLock()
        self.ffprobe = _binary("ffprobe")
        self.ffmpeg = _binary("ffmpeg")
        for name, index in (("video.jsonl", self.metadata), ("video_resources.jsonl", self.resources)):
            p = self.metadata_dir / name
            if p.is_file():
                for line in p.read_text(encoding="utf-8-sig").splitlines():
                    try:
                        row = json.loads(line); index[_identity(row)] = row
                    except ValueError:
                        continue

    def resolve(self, row, make_previews=False, resource_rows=None):
        with self._lock:
            try:
                identity = _identity(row)
            except IdentityError as exc:
                return {"status": "identity_unavailable", "videos": [], "reason": str(exc),
                        "network_used": False, "whole_video_watched": False}
            rejected = False
            for resource in resource_rows or []:
                try:
                    key = _identity(resource)
                except IdentityError:
                    rejected = True
                    continue
                self.resources[key] = resource
            original = self.metadata.get(identity, row.get('original', row))
            resource = self.resources.get(identity, {})
            ids = {str(item.get("md5", "")).lower() for item in resource.get("resources", [])
                   if item.get("resource_family") == "video" and HEX.fullmatch(str(item.get("md5", "")))}
            if not self.account:
                return {"status": "account_unconfigured", "videos": []}
            if not ids:
                return {"status": "identity_unavailable" if rejected else "metadata_missing", "videos": [],
                        **({"reason": "unscoped_resource_identity"} if rejected else {})}
            timestamp = int(identity[3])
            month = dt.datetime.fromtimestamp(timestamp, TZ).strftime("%Y-%m")
            video_root = self.account / "msg/video"
            candidates = []
            covers = []
            for resource_id in sorted(ids):
                exact = video_root / month / (resource_id + ".mp4")
                if exact.is_file():
                    candidates.append(exact)
                else:
                    # A forwarded resource may retain its original storage month;
                    # search this account's video month folders for this ID only.
                    candidates.extend(video_root.glob("*/" + resource_id + ".mp4"))
                for suffix in ("_thumb.jpg", ".jpg"):
                    cover = video_root / month / (resource_id + suffix)
                    if cover.is_file() and validate_image_path(cover)["valid"]:
                        covers.append(str(cover))
            videos = []
            failures = []
            for path in sorted(set(candidates)):
                if not path.resolve().is_relative_to(video_root.resolve()):
                    failures.append({"status": "source_out_of_scope"}); continue
                stat = path.stat(); key = (str(path), stat.st_size, stat.st_mtime_ns)
                if key not in self._probes:
                    self._probes[key] = probe_video(path, ffprobe=self.ffprobe, ffmpeg=self.ffmpeg)
                verdict = self._probes[key]
                if not verdict["valid"]:
                    failures.append({"source_path": str(path), **verdict}); continue
                ref = {"path": str(path), "uri": path.as_uri(), "resource_family": "video", "direct_readable": True,
                       "provenance": "exact_message_resource_identity", "media_validation": verdict}
                if make_previews:
                    ref["previews"] = preview_frames(path, verdict, ffmpeg=self.ffmpeg)
                videos.append(ref)
            return {"status": "playable_local" if videos else "video_unavailable" if candidates else "source_missing",
                    "videos": videos, "cover_paths": covers, "local_candidates": len(set(candidates)),
                    "failures": failures, "network_used": False, "whole_video_watched": False}

    def enrich_payload(self, payload, make_previews=False, resource_rows=None):
        def walk(value):
            if isinstance(value, list):
                return [walk(v) for v in value]
            if not isinstance(value, dict):
                return value
            if (value.get("kind") or value.get("kind_name")) == "video" and any(k in value for k in ("id", "server_id", "server_id_str", "message_id")):
                output = copy.deepcopy(value)
                result = self.resolve(value, make_previews, resource_rows)
                output["wechat_video_resolution"] = {k: v for k, v in result.items() if k != "videos"}
                if result["videos"]:
                    output["videos"] = result["videos"]
                elif result['status'] in {'identity_unavailable', 'identity_ambiguous'}:
                    output.pop('videos', None)
                return output
            return {k: walk(v) for k, v in value.items()}
        return walk(payload)


_resolver = None
_lock = threading.Lock()


def enrich_wechat_video(payload, resource_rows=None, make_previews=False):
    global _resolver
    with _lock:
        if _resolver is None:
            _resolver = WeChatVideoResolver()
    return _resolver.enrich_payload(payload, make_previews, resource_rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--previews", action="store_true")
    args = parser.parse_args()
    resolver = WeChatVideoResolver()
    counts = Counter()
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.open("w", encoding="utf-8") as stream:
        for n, row in enumerate(resolver.metadata.values(), 1):
            result = resolver.resolve(row, args.previews)
            counts[result["status"]] += 1
            stream.write(json.dumps({"identity": _identity(row), **result}, ensure_ascii=False) + "\n")
            stream.flush()
            if n % 25 == 0:
                print(json.dumps({"processed": n, "statuses": counts}), flush=True)
    summary = {"processed": sum(counts.values()), "statuses": dict(counts), "ffprobe_available": bool(resolver.ffprobe),
               "ffmpeg_available": bool(resolver.ffmpeg), "network_used": False, "whole_video_watched": False}
    args.manifest.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
