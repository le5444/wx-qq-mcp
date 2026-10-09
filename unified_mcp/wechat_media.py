"""Resolve local WeChat pictures/stickers without modifying WeChat data.

Only exact content digests or the message's resource identity associate files.
V4 candidates use existing local keys, format trailers, or a verified local
DAT/plaintext pair; there is no key brute force and no network access. All
animation frames must decode. Recovered bytes are deduplicated in runtime.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import threading
import time
import xml.etree.ElementTree as ET

from unified_mcp.media_validation import _decode_bytes, _read_local_image, validate_image_path, IMAGE_EXTENSIONS
from unified_mcp.runtime_paths import RUNTIME

BASE = Path(__file__).resolve().parent
HEX = re.compile(r"^[a-fA-F0-9]{32}$")
FILE_ID = re.compile(r"^([a-fA-F0-9]{32})(?:_(?:h|t|hd))?(?:-[a-fA-F0-9]{16})?(?:\.[^.]+)?$")
EXT = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp", "BMP": ".bmp", "TIFF": ".tiff", "AVIF": ".avif"}
V4 = (b"\x07\x08V1\x08\x07", b"\x07\x08V2\x08\x07")


def _identity(row):
    if isinstance(row.get("original"), dict):
        return _identity(row["original"])
    ident = row.get("id") if isinstance(row.get("id"), dict) else {}
    server = next((v for v in (row.get("server_id_str"), row.get("message_id"), ident.get("server_id_str"), row.get("server_id")) if v is not None and v != ""), 0)
    return (str(row.get("talker") or ident.get("talker") or row.get("chat_id") or ""),
            str(server),
            str(row.get("local_id") or ident.get("local_id") or ""),
            str(row.get("create_time") or row.get("timestamp") or ""))


def _parsed(row):
    parsed = row.get("message_content_parsed")
    if isinstance(parsed, dict):
        return parsed
    text = row.get("message_content", "")
    if not isinstance(text, str) or len(text) > 2_000_000 or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        return {}
    try:
        root = ET.fromstring(text)
        child = root.find("emoji")
        if child is None:
            child = root.find("img")
        return dict(child.attrib) if child is not None else {}
    except (ET.ParseError, ValueError):
        return {}


def _files(root):
    """Never follow directory links outside the explicitly selected tree."""
    if not root.is_dir():
        return
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
        for name in names:
            p = Path(base) / name
            if not p.is_symlink():
                yield p


class WeChatMediaResolver:
    def __init__(self, account_root=None, data_root=None, metadata_dir=None,
                 output_dir=None, config_path=None, max_new_bytes=3 * 1024**3):
        cfgpath = Path(config_path or os.environ.get("WECHAT_CLI_CONFIG") or os.environ.get("WX_MCP_CONFIG") or Path.home() / ".config/wxcli/config.json")
        try:
            cfg = json.loads(cfgpath.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            cfg = {}
        configured_root = account_root or os.environ.get("WECHAT_CLI_DB_ROOT") or os.environ.get("WX_MCP_DB_ROOT") or cfg.get("db_root")
        self.account = Path(configured_root).resolve() if configured_root else None
        self.data_root = Path(data_root).resolve() if data_root else self.account.parent if self.account else Path.home() / "xwechat_files"
        self.metadata_dir = Path(metadata_dir or os.environ.get("UNIFIED_MEDIA_METADATA_DIR") or RUNTIME / "media_metadata")
        self.output = Path(output_dir or RUNTIME / "wechat-media").resolve()
        if not self.output.is_relative_to(RUNTIME.resolve()):
            raise ValueError("Recovered output must remain within WXQQ_DATA_DIR")
        self.max_new_bytes = max_new_bytes
        self.new_bytes = 0
        self._keys = []
        for raw in [os.environ.get("WECHAT_CLI_IMAGE_KEY"), os.environ.get("WX_MCP_IMAGE_KEY"), cfg.get("image_key")]:
            if isinstance(raw, str):
                if len(raw.encode()) >= 16:
                    self._keys.append(raw.encode()[:16])
                try:
                    decoded = bytes.fromhex(raw)
                    if len(decoded) >= 16:
                        self._keys.append(decoded[:16])
                except ValueError:
                    pass
        self._keys = list(dict.fromkeys(self._keys))
        self._xor = set()
        self._metadata = {}
        self._resources = defaultdict(list)
        self._metadata_mtime = {}
        self._plaintext = defaultdict(list)
        self._named = defaultdict(list)
        self._indexed_chats = {}
        self._global_indexed = 0
        account_tag = hashlib.sha256(str(self.account).encode()).hexdigest()[:16]
        self._index_path = RUNTIME / ("wechat-content-index-" + account_tag + ".json")
        try:
            self._content_cache = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._content_cache = {}
        self._verdicts = {}
        self._resolved = {}
        self._lock = threading.RLock()

    def load_metadata(self):
        for name in ("image.jsonl", "sticker.jsonl", "image_resources.jsonl"):
            p = self.metadata_dir / name
            try:
                stat = p.stat()
            except OSError:
                continue
            stamp = (stat.st_size, stat.st_mtime_ns)
            if self._metadata_mtime.get(name) == stamp:
                continue
            with p.open(encoding="utf-8-sig") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue  # A writer may still be appending the last line.
                    if name == "image_resources.jsonl":
                        key = _identity(row)
                        if row not in self._resources[key]:
                            self._resources[key].append(row)
                    else:
                        self._metadata[_identity(row)] = row
            self._metadata_mtime[name] = stamp

    def _index_plain(self, path):
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            return
        try:
            stat = path.stat()
        except OSError:
            return
        entry = self._content_cache.get(str(path))
        if not entry or entry.get("size") != stat.st_size or entry.get("mtime_ns") != stat.st_mtime_ns:
            data, error = _read_local_image(path)
            if error is not None:
                return
            entry = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "md5": hashlib.md5(data).hexdigest()}
            self._content_cache[str(path)] = entry
        if path not in self._plaintext[entry["md5"]]:
            self._plaintext[entry["md5"]].append(path)
        match = FILE_ID.match(path.name)
        if match and path not in self._named[match[1].lower()]:
            self._named[match[1].lower()].append(path)

    def _index(self, talker):
        if time.monotonic() - self._global_indexed > 300:
            for root in (self.data_root / "Emojis", self.account / "temp" if self.account else self.data_root / "__missing__",
                         RUNTIME / "sticker-download"):
                for path in _files(root):
                    self._index_plain(path)
            self._global_indexed = time.monotonic()
        if time.monotonic() - self._indexed_chats.get(talker, 0) < 300:
            return
        if not re.fullmatch(r"[\w@.-]+", talker):
            return
        for path in _files(self.data_root / "Images" / talker):
            self._index_plain(path)
        if self.account:
            target = self.account / "msg/attach" / hashlib.md5(talker.encode()).hexdigest()
            for path in _files(target):
                match = FILE_ID.match(path.name)
                if match and path.suffix.lower() == ".dat" and path not in self._named[match[1].lower()]:
                    self._named[match[1].lower()].append(path)
            # Cache copies already decoded by wx-mcp are only associated later
            # through the exact resource ID; scanning never changes these files.
            wxid = self.account.name.rsplit("_", 1)[0]
            for name in (".wx-mcp", ".wechat-cli"):
                for path in _files(Path.home() / name / "media-cache" / wxid):
                    match = FILE_ID.match(path.name)
                    if match and path.suffix.lower() in IMAGE_EXTENSIONS and path not in self._named[match[1].lower()]:
                        self._named[match[1].lower()].append(path)
        self._indexed_chats[talker] = time.monotonic()
        self._learn_verified_pairs()
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(json.dumps(self._content_cache), encoding="utf-8")

    def _learn_verified_pairs(self):
        """Use prior strict repair output only when DAT middle/tail correspond."""
        for p in _files(RUNTIME / "media-repair"):
            match = FILE_ID.match(p.name)
            if not match:
                continue
            plain, error = _read_local_image(p)
            if error or not _decode_bytes(plain)["valid"]:
                continue
            expected_digest = p.stem.rsplit("-", 1)[-1]
            if hashlib.sha256(plain).hexdigest()[:16] != expected_digest:
                continue
            stem = p.stem.rsplit("-", 1)[0]
            for source in self._named[match[1].lower()]:
                if source.suffix.lower() != ".dat" or source.stem != stem:
                    continue
                data, error = _read_local_image(source)
                if error or data[:6] not in V4 or len(data) < 15:
                    continue
                aes_len, xor_len = struct.unpack("<II", data[6:14])
                pad = 16 - aes_len % 16 if aes_len else 0
                if len(plain) != len(data) - 15 - pad or not 0 < xor_len <= len(plain) - aes_len:
                    continue
                if plain[aes_len:-xor_len] != data[15 + aes_len + pad:-xor_len]:
                    continue
                value = plain[-1] ^ data[-1]
                if all((a ^ b) == value for a, b in zip(plain[-xor_len:], data[-xor_len:])):
                    # Also authenticate the AES prefix using the loaded key.
                    prefixes = self._prefixes(data, aes_len)
                    if plain[:aes_len] in prefixes:
                        self._xor.add(value)
                        if p not in self._named[match[1].lower()]:
                            self._named[match[1].lower()].append(p)

    def _prefixes(self, data, aes_len):
        if not aes_len:
            return [b""]
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            return []
        length = aes_len + 16 - aes_len % 16
        if 15 + length > len(data):
            return []
        keys = [b"cfcd208495d565ef"] if data[:6] == V4[0] else self._keys
        out = []
        for key in keys:
            decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            plain = decryptor.update(data[15:15 + length]) + decryptor.finalize()
            pad = plain[-1]
            if 0 < pad <= 16 and plain[-pad:] == bytes([pad]) * pad and len(plain) - pad == aes_len:
                out.append(plain[:-pad])
        return list(dict.fromkeys(out))

    def _decode_dat(self, path, expected_md5=None):
        data, error = _read_local_image(path)
        if error:
            return [], error["status"]
        candidates = []
        if len(data) >= 15 and data[:6] in V4:
            aes_len, xor_len = struct.unpack("<II", data[6:14])
            length = aes_len + (16 - aes_len % 16 if aes_len else 0)
            if 15 + length + xor_len > len(data):
                return [], "invalid_dat_lengths"
            prefixes = self._prefixes(data, aes_len)
            if not prefixes:
                return [], "aes_prefix_unavailable"
            middle = data[15 + length:len(data) - xor_len] if xor_len else data[15 + length:]
            tail = data[-xor_len:] if xor_len else b""
            xor_values = set(self._xor) if xor_len else {0}
            # Derive candidates from complete known trailers, never 256 values.
            for trailer in (b"\xff\xd9", b"\x00\x00\x00\x00IEND\xaeB`\x82"):
                if len(tail) >= len(trailer):
                    deltas = {a ^ b for a, b in zip(tail[-len(trailer):], trailer)}
                    if len(deltas) == 1:
                        xor_values.update(deltas)
            for prefix in prefixes:
                for value in xor_values:
                    plain = prefix + middle + tail.translate(bytes(x ^ value for x in range(256)))
                    candidates.append(plain)
        else:
            # V3 XOR derives a single value per recognized header.
            for header in (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF"):
                if len(data) >= len(header):
                    deltas = {a ^ b for a, b in zip(data[:len(header)], header)}
                    if len(deltas) == 1:
                        value = deltas.pop()
                        candidates.append(data.translate(bytes(x ^ value for x in range(256))))
        found = {}
        for plain in candidates:
            verdict = _decode_bytes(plain)
            if verdict["valid"]:
                found[hashlib.sha256(plain).hexdigest()] = (plain, verdict)
        # An exact XML content MD5 proves which candidate belongs to the message.
        exact = {k: v for k, v in found.items() if hashlib.md5(v[0]).hexdigest() == expected_md5}
        if exact:
            found = exact
        if len(found) > 1:
            return [], "ambiguous_decoding"
        return list(found.values()), "decoded" if found else "decode_failed"

    def _ref(self, path, evidence, expected_md5=None):
        try:
            stamp = (str(path), path.stat().st_size, path.stat().st_mtime_ns)
        except OSError:
            return None
        if stamp not in self._verdicts:
            self._verdicts[stamp] = validate_image_path(path)
        verdict = self._verdicts[stamp]
        if not verdict["valid"]:
            return None
        digest = hashlib.md5(path.read_bytes()).hexdigest()
        stem = path.stem.rsplit("-", 1)[0]
        variant = "thumbnail" if stem.endswith("_t") else "high" if stem.endswith(("_h", "_hd")) else "image"
        return {"path": str(path), "uri": path.as_uri(), "direct_readable": True,
                "resource_family": "image", "provenance": evidence,
                "variant": variant,
                "content_md5": digest, "xml_md5_matches": bool(expected_md5 and digest == expected_md5),
                "media_validation": verdict}

    def _save(self, plain, verdict):
        digest = hashlib.sha256(plain).hexdigest()
        ext = EXT.get(verdict["format"])
        if not ext:
            return None, "unsupported_output_format"
        path = self.output / (digest + ext)
        if path.exists():
            if path.read_bytes() != plain:
                return None, "output_collision"
            return path, "reused_recovered_copy"
        if self.new_bytes + len(plain) > self.max_new_bytes:
            return None, "output_budget_reached"
        self.output.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(plain)
        except FileExistsError:
            if path.read_bytes() != plain:
                return None, "output_collision"
        else:
            self.new_bytes += len(plain)
        return path, "recovered_copy"

    def resolve(self, row):
        with self._lock:
            return self._resolve(row)

    def _resolve(self, row):
        identity = _identity(row)
        original = self._metadata.get(identity, row)
        kind = original.get("kind_name") or original.get("kind") or row.get("kind")
        if kind not in ("image", "sticker", "emoji"):
            return {"status": "not_applicable", "images": []}
        parsed = _parsed(original)
        md5 = str(parsed.get("md5", "")).lower()
        linked_resources = [resource for record in self._resources.get(identity, [])
                            for resource in [record, *record.get("resources", [])]]
        linked_resources.extend(original.get("media_resources", []) or [])
        has_storage_md5 = any(HEX.fullmatch(str(resource.get("md5", ""))) for resource in linked_resources)
        if not HEX.fullmatch(md5):
            existing = []
            for item in row.get("images", []) or []:
                value = item.get("path") if isinstance(item, dict) else item
                if not isinstance(value, str) or not value or "://" in value or value.startswith(("\\\\", "//")):
                    continue
                path = Path(value)
                if not path.is_absolute():
                    continue
                ref = self._ref(path, "existing_backend_reference")
                if ref:
                    existing.append(ref)
            if existing:
                return {"status": "readable", "images": existing, "kind": kind,
                        "metadata_status": "missing", "network_used": False}
            if kind != "image" or not has_storage_md5:
                return {"status": "metadata_missing", "images": [], "kind": kind}
            # Some genuine image messages contain an empty XML md5. The exact
            # resource join still identifies their DAT; do not discard it.
            md5 = ""
        talker = identity[0]
        self._index(talker)
        refs = []
        for p in self._plaintext.get(md5, []):
            ref = self._ref(p, "xml_content_md5", md5)
            if ref and ref["xml_md5_matches"]:
                refs.append(ref)
                break  # Identical digest copies need only one readable path.
        if refs:
            return {"status": "readable", "images": refs, "kind": kind, "md5": md5, "network_used": False}
        if kind in ("sticker", "emoji"):
            return {"status": "local_file_missing", "images": [], "kind": kind, "md5": md5,
                    "remote_reference_present": bool(parsed.get("cdn_url") or parsed.get("cdnurl") or parsed.get("encrypt_url")),
                    "network_used": False}
        # Content MD5 may itself be the storage filename; additional IDs are
        # accepted only from resource records already joined by the server ID.
        resource_ids = {md5} if md5 else set()
        for resource in linked_resources:
            for field in ("md5", "content_md5"):
                value = str(resource.get(field, "")).lower()
                if HEX.fullmatch(value):
                    resource_ids.add(value)
        candidates = []
        for resource_id in sorted(resource_ids):
            candidates.extend(self._named.get(resource_id, []))
        failures = Counter()
        seen = set()
        # Full/high-resolution images precede thumbnails; keep one per variant.
        candidates = sorted(set(candidates), key=lambda p: ("_t" in p.stem, p.suffix.lower() == ".dat", str(p)))
        for p in candidates:
            if p.suffix.lower() != ".dat":
                ref = self._ref(p, "message_resource_filename", md5)
                # Historical wx-mcp caches only checked the header, so a
                # decodable cache with a different content digest is not enough
                # to certify a particular XOR candidate. Re-decode the DAT.
                if ref and not ref["xml_md5_matches"] and not p.is_relative_to(self.data_root / "Images") and not p.is_relative_to(RUNTIME / "media-repair"):
                    continue
                if ref:
                    if ref["content_md5"] not in seen:
                        refs.append(ref); seen.add(ref["content_md5"])
                continue
            cache_key = (str(p), p.stat().st_size, p.stat().st_mtime_ns, md5)
            if cache_key in self._resolved:
                cached = self._resolved[cache_key]
                if cached and cached["content_md5"] not in seen:
                    refs.append(cached); seen.add(cached["content_md5"])
                continue
            decoded, status = self._decode_dat(p, md5)
            if not decoded:
                failures[status] += 1
                continue
            plain, verdict = decoded[0]
            content_md5 = hashlib.md5(plain).hexdigest()
            if content_md5 in seen:
                continue
            destination, saved = self._save(plain, verdict)
            if destination is None:
                failures[saved] += 1
                continue
            ref = self._ref(destination, "message_resource_dat", md5)
            if ref:
                ref.update({"source_dat_path": str(p), "source_unchanged": True, "recovery_status": saved,
                            "variant": "thumbnail" if p.stem.endswith("_t") else "high" if p.stem.endswith("_h") else "image"})
                refs.append(ref); seen.add(content_md5)
                self._resolved[cache_key] = ref
        return {"status": "readable" if refs else "unavailable", "images": refs, "kind": kind, "md5": md5,
                "content_md5_available": bool(md5),
                "only_thumbnail": bool(refs) and all(ref.get("variant") == "thumbnail" for ref in refs),
                "local_candidates": len(candidates), "failures": dict(failures), "network_used": False}

    def enrich_payload(self, payload, metadata_rows=None, resource_rows=None):
        with self._lock:
            self.load_metadata()
            for row in metadata_rows or []:
                self._metadata[_identity(row)] = row
            for row in resource_rows or []:
                key = _identity(row)
                if row not in self._resources[key]:
                    self._resources[key].append(row)
            def walk(value):
                if isinstance(value, list):
                    return [walk(v) for v in value]
                if not isinstance(value, dict):
                    return value
                kind = value.get("kind_name") or value.get("kind")
                if kind in ("image", "sticker", "emoji") and any(k in value for k in ("id", "server_id", "server_id_str", "message_id")):
                    out = copy.deepcopy(value)
                    resolution = self._resolve(value)
                    out["wechat_media_resolution"] = {k: v for k, v in resolution.items() if k != "images"}
                    if resolution["images"]:
                        out["images"] = resolution["images"]
                    return out
                return {k: walk(v) for k, v in value.items()}
            return walk(payload)


_resolver = None
_resolver_lock = threading.Lock()


def enrich_wechat_media(payload, metadata_rows=None, resource_rows=None):
    """Synchronous gateway hook: call with asyncio.to_thread before sanitizing."""
    global _resolver
    with _resolver_lock:
        if _resolver is None:
            _resolver = WeChatMediaResolver()
    return _resolver.enrich_payload(payload, metadata_rows, resource_rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    resolver = WeChatMediaResolver()
    resolver.load_metadata()
    counts = Counter()
    count = 0
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.input.open(encoding="utf-8-sig") as stream, args.manifest.open("w", encoding="utf-8") as out:
        for line in stream:
            row = json.loads(line)
            result = resolver.resolve(row)
            out.write(json.dumps({"identity": _identity(row), **result}, ensure_ascii=False) + "\n")
            out.flush()
            count += 1
            counts[result["status"]] += 1
            if count % 100 == 0:
                print(json.dumps({"processed": count, "statuses": counts, "new_bytes": resolver.new_bytes}), flush=True)
            if args.limit and count >= args.limit:
                break
    summary = {"processed": count, "statuses": dict(counts), "new_bytes": resolver.new_bytes, "network_used": False}
    args.manifest.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
