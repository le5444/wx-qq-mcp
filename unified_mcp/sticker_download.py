"""Fetch explicitly selected missing WeChat stickers from their Tencent URLs.

Downloads are opt-in, bounded, and never run as a side effect of a chat query.
Only full image decodes with the exact message XML MD5 enter the local cache.
Reports contain host names and status codes, never source URLs or tokens.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from unified_mcp.media_validation import _decode_bytes
from unified_mcp.runtime_paths import RUNTIME


BASE = Path(__file__).resolve().parent
MAX_FILE_BYTES = 10 * 1024**2
MAX_TOTAL_BYTES = 100 * 1024**2
OFFICIAL_DOMAINS = ("qq.com", "qpic.cn", "qlogo.cn")
HEX = re.compile(r"[a-fA-F0-9]{32}")
EXTENSIONS = {"GIF": ".gif", "PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp",
              "BMP": ".bmp", "TIFF": ".tiff", "AVIF": ".avif"}
REDIRECTS = {301, 302, 303, 307, 308}
TUN_FAKE_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48"))


class FetchFailure(Exception):
    def __init__(self, status, **details):
        super().__init__(status)
        self.status = status
        self.details = details


def allowed_host(url):
    """Validate the URL without returning its private path/query to reports."""
    if not isinstance(url, str) or len(url) > 16384 or any(ord(c) < 32 for c in url):
        raise FetchFailure("blocked_url")
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        raise FetchFailure("blocked_url") from None
    if parts.scheme.lower() not in {"http", "https"} or not host or parts.username is not None or parts.password is not None:
        raise FetchFailure("blocked_url")
    if port not in (None, 80, 443):
        raise FetchFailure("blocked_url")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise FetchFailure("blocked_url")
    if not any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS):
        raise FetchFailure("blocked_url")
    return host, port or (443 if parts.scheme.lower() == "https" else 80)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Every Location is checked before a new GET is issued.


def network_failure(exc):
    cause = getattr(exc, "reason", exc)
    cause = cause if isinstance(cause, BaseException) else exc
    details = {"error_type": type(cause).__name__}
    for field in ("errno", "verify_code"):
        value = getattr(cause, field, None)
        if isinstance(value, int):
            details[field] = value
    return FetchFailure("network_error", **details)


def decrypt_xml_media(data, key_hex, expected_md5):
    """Use the XML's explicit key and accept only its declared plaintext MD5.

    Legacy emoji: AES-CBC with IV equal to the key, observed in the author's
    protocol implementation at https://blog.seeflower.dev/archives/182/ .
    Current Tencent CDN media also uses AES-ECB/PKCS7. These two fixed formats
    share no inferred keys; an exact expected digest decides applicability.
    """
    if not isinstance(key_hex, str) or not HEX.fullmatch(key_hex) or not data or len(data) % 16:
        raise FetchFailure("invalid_encryption_parameters")
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
    except ImportError:
        raise FetchFailure("aes_decoder_unavailable") from None
    key = bytes.fromhex(key_hex)
    for label, cipher in (("AES-128-CBC-IV=XML-key", AES.new(key, AES.MODE_CBC, iv=key)),
                          ("AES-128-ECB", AES.new(key, AES.MODE_ECB))):
        plain = cipher.decrypt(data)
        candidates = [(plain, "unmodified")]
        try:
            candidates.insert(0, (unpad(plain, 16), "PKCS7"))
        except ValueError:
            pass
        for candidate, padding in candidates:
            if hashlib.md5(candidate).hexdigest() == expected_md5:
                return candidate, label + "-" + padding
    raise FetchFailure("decrypted_md5_mismatch")


class StickerDownloader:
    def __init__(self, output_dir=None, *, opener=None, resolver=None, timeout=20,
                 max_file_bytes=MAX_FILE_BYTES, max_total_bytes=MAX_TOTAL_BYTES, https_tun=False,
                 original_http_tun=False):
        self.output = Path(output_dir or RUNTIME / "sticker-download").resolve()
        if not self.output.is_relative_to(RUNTIME.resolve()):
            raise ValueError("Sticker downloads must remain within WXQQ_DATA_DIR")
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.resolver = resolver or socket.getaddrinfo
        self.timeout = min(float(timeout), 20)
        self.max_file_bytes = min(int(max_file_bytes), MAX_FILE_BYTES)
        self.max_total_bytes = min(int(max_total_bytes), MAX_TOTAL_BYTES)
        if min(self.timeout, self.max_file_bytes, self.max_total_bytes) <= 0:
            raise ValueError("Download limits must be positive")
        self.used_bytes = 0
        self.https_tun = bool(https_tun)
        self.original_http_tun = bool(original_http_tun)
        if self.https_tun and self.original_http_tun:
            raise ValueError("Choose one explicit TUN protocol mode")

    def _public_address(self, host, port):
        try:
            addresses = self.resolver(host, port, type=socket.SOCK_STREAM)
            ips = [ipaddress.ip_address(entry[4][0]) for entry in addresses]
            permitted = ips and all(ip.is_global or ((self.https_tun or self.original_http_tun) and any(ip in net for net in TUN_FAKE_NETWORKS if net.version == ip.version)) for ip in ips)
        except (OSError, ValueError):
            raise FetchFailure("dns_error") from None
        if not permitted:
            raise FetchFailure("blocked_address")

    def _fetch(self, url, result):
        deadline = time.monotonic() + self.timeout
        for redirects in range(4):
            host, port = allowed_host(url)
            if self.https_tun and urllib.parse.urlsplit(url).scheme.lower() != "https":
                parts = urllib.parse.urlsplit(url)
                url = urllib.parse.urlunsplit(("https", host, parts.path, parts.query, ""))
                port = 443
                result["https_upgrade"] = True
            self._public_address(host, port)
            result["source_host"] = host
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise FetchFailure("timeout")
            if self.used_bytes >= self.max_total_bytes:
                raise FetchFailure("total_byte_limit")
            request = urllib.request.Request(url, method="GET", headers={
                "User-Agent": "LocalStickerReader/1.0", "Accept-Encoding": "identity"})
            result["network_used"] = True
            result["requests"] += 1
            try:
                response = self.opener.open(request, timeout=remaining_time)
            except urllib.error.HTTPError as exc:
                response = exc
            except (TimeoutError, socket.timeout):
                raise FetchFailure("timeout") from None
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
                raise network_failure(exc) from None
            try:
                status = response.status if hasattr(response, "status") else response.code
                if status in REDIRECTS:
                    location = response.headers.get("Location")
                    if not location or redirects == 3:
                        raise FetchFailure("redirect_limit" if redirects == 3 else "redirect_without_location")
                    url = urllib.parse.urljoin(url, location)
                    allowed_host(url)
                    continue
                if status != 200:
                    raise FetchFailure("http_error", http_status=int(status))
                length = response.headers.get("Content-Length")
                try:
                    length = int(length) if length is not None else None
                except ValueError:
                    raise FetchFailure("invalid_content_length") from None
                available = min(self.max_file_bytes, self.max_total_bytes - self.used_bytes)
                if length is not None and (length < 0 or length > available):
                    raise FetchFailure("file_byte_limit" if length > self.max_file_bytes else "total_byte_limit")
                chunks = []
                received = 0
                while True:
                    if time.monotonic() >= deadline:
                        raise FetchFailure("timeout")
                    if length is not None and received == length:
                        break
                    capacity = min(self.max_file_bytes - received, self.max_total_bytes - self.used_bytes)
                    if capacity <= 0:
                        raise FetchFailure("file_byte_limit" if received >= self.max_file_bytes else "total_byte_limit")
                    # read1 returns currently available data, so a slow stream
                    # cannot postpone deadline checks until a whole chunk fills.
                    reader = getattr(response, "read1", response.read)
                    transport = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if transport is not None:
                        transport.settimeout(max(0.001, deadline - time.monotonic()))
                    chunk = reader(min(65536, capacity))
                    if not chunk:
                        if length is not None and received != length:
                            raise FetchFailure("incomplete_response")
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                    self.used_bytes += len(chunk)
                return b"".join(chunks)
            except (TimeoutError, socket.timeout):
                raise FetchFailure("timeout") from None
            except (OSError, urllib.error.URLError) as exc:
                raise network_failure(exc) from None
            except http.client.HTTPException:
                raise FetchFailure("incomplete_response") from None
            finally:
                response.close()
        raise FetchFailure("redirect_limit")

    @staticmethod
    def _validate(data, expected):
        if hashlib.md5(data).hexdigest() != expected:
            raise FetchFailure("md5_mismatch")
        verdict = _decode_bytes(data)
        if not verdict["valid"]:
            raise FetchFailure("image_validation_failed", validation_status=verdict["status"],
                               decoded_container="wxgf" if data.startswith(b"wxgf") else "unrecognized_or_invalid")
        if verdict["format"] not in EXTENSIONS:
            raise FetchFailure("unsupported_image_format")
        return verdict

    def download(self, expected_md5, url, *, aes_key=None):
        result = {"md5": str(expected_md5).lower(), "network_used": False, "requests": 0}
        initial_bytes = self.used_bytes
        try:
            if not HEX.fullmatch(result["md5"]):
                raise FetchFailure("invalid_md5")
            for extension in EXTENSIONS.values():
                existing = self.output / (result["md5"] + extension)
                if existing.exists():
                    if not existing.resolve().is_relative_to(self.output) or not existing.is_file() or existing.stat().st_size > self.max_file_bytes:
                        raise FetchFailure("existing_cache_conflict")
                    data = existing.read_bytes()
                    try:
                        verdict = self._validate(data, result["md5"])
                    except FetchFailure:
                        raise FetchFailure("existing_cache_conflict") from None
                    result.update(path=str(existing), bytes=len(data),
                                  cache_hit=True, **verdict)
                    result["status"] = "available"
                    return result
            if not url:
                raise FetchFailure("no_remote_reference")
            data = self._fetch(url, result)
            if hashlib.md5(data).hexdigest() != result["md5"] and aes_key:
                data, result["decryption"] = decrypt_xml_media(data, aes_key, result["md5"])
            verdict = self._validate(data, result["md5"])
            self.output.mkdir(parents=True, exist_ok=True)
            destination = self.output / (result["md5"] + EXTENSIONS[verdict["format"]])
            temporary = self.output / (result["md5"] + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as target:
                    target.write(data)
                try:
                    os.link(temporary, destination)  # Atomic, with no overwrite.
                except FileExistsError:
                    if not destination.resolve().is_relative_to(self.output) or destination.read_bytes() != data:
                        raise FetchFailure("existing_cache_conflict") from None
            finally:
                temporary.unlink(missing_ok=True)
            result.update(path=str(destination), bytes=len(data), cache_hit=False, **verdict)
            result["status"] = "available"
            return result
        except FetchFailure as exc:
            result.update(status=exc.status, **exc.details)
            return result
        except OSError:
            result["status"] = "local_io_error"
            return result
        finally:
            result["network_bytes"] = self.used_bytes - initial_bytes


def missing_groups(metadata, resolved):
    missing = {tuple(row["identity"]) for row in resolved if row.get("status") == "local_file_missing"}
    groups = {}
    for row in sorted(metadata, key=lambda item: item.get("create_time", 0), reverse=True):
        identity = (str(row.get("talker") or ""), str(row.get("server_id_str") or row.get("server_id") or 0),
                    str(row.get("local_id") or ""), str(row.get("create_time") or ""))
        if identity not in missing:
            continue
        parsed = row.get("message_content_parsed") or {}
        digest = str(parsed.get("md5") or "").lower()
        entry = groups.setdefault(digest, {"md5": digest, "identities": [], "url": None, "url_field": None, "aes_key": None})
        entry["identities"].append(identity)
        if not entry["url"]:
            for field in ("cdn_url", "cdnurl", "encrypt_url", "encrypturl"):
                if isinstance(parsed.get(field), str) and parsed[field].startswith(("http://", "https://")):
                    entry["url"] = parsed[field]
                    entry["url_field"] = field
                    entry["aes_key"] = parsed.get("aeskey") if field.startswith("encrypt") else None
                    break
    return list(groups.values())


def read_jsonl(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def run(options):
    groups = missing_groups(read_jsonl(options.metadata), read_jsonl(options.resolved))
    downloader = StickerDownloader(options.output, https_tun=getattr(options, "https_tun", False),
                                   original_http_tun=getattr(options, "original_http_tun", False))
    downloader.output.mkdir(parents=True, exist_ok=True)
    ledger = downloader.output / "download_results.jsonl"
    previous = read_jsonl(ledger) if ledger.exists() else []
    known = {row["md5"]: row for row in previous}
    downloader.used_bytes = sum(row.get("network_bytes", 0) for row in previous)
    processed = 0
    with ledger.open("a", encoding="utf8") as target:
        for group in groups:
            prior = known.get(group["md5"])
            correction = False
            legacy_correction = False
            if prior:  # Network failures are never retried silently.
                recheck = getattr(options, "recheck_unrequested", False) and not prior.get("network_used") and prior.get("requests", 0) == 0 and prior.get("status") == "blocked_address"
                if getattr(options, "correct_protocol_and_encryption", False) and not prior.get("correction_applied"):
                    correction = ((prior.get("status") == "network_error" and prior.get("https_upgrade")
                                   and group["url"] and urllib.parse.urlsplit(group["url"]).scheme == "http")
                                  or (prior.get("status") == "md5_mismatch" and group["aes_key"]
                                      and str(group["url_field"]).startswith("encrypt")))
                legacy_correction = (getattr(options, "legacy_sticker_cbc_correction", False)
                                     and not prior.get("legacy_correction_applied")
                                     and prior.get("status") == "encrypted_payload_invalid"
                                     and bool(group["aes_key"]) and str(group["url_field"]).startswith("encrypt"))
                if not recheck and not correction and not legacy_correction:
                    continue
            if options.limit is not None and processed >= options.limit:
                break
            result = downloader.download(group["md5"], group["url"], aes_key=group["aes_key"])
            result.update(identities=group["identities"], message_references=len(group["identities"]),
                          url_field=group["url_field"])
            if correction:
                result["correction_applied"] = True
                result["previous_status"] = prior["status"]
            if legacy_correction:
                result["legacy_correction_applied"] = True
                result["previous_status"] = prior["status"]
            target.write(json.dumps(result, ensure_ascii=False) + "\n")
            target.flush()
            known[group["md5"]] = result
            processed += 1
            print(f"sticker {len(known)}/{len(groups)}: {result['status']}", flush=True)
    selected = [known[group["md5"]] for group in groups if group["md5"] in known]
    summary = {"requested_message_references": sum(len(group["identities"]) for group in groups),
               "unique_md5": len(groups), "processed_unique_md5": len(selected),
               "complete": len(selected) == len(groups),
               "available_unique_files": sum(row["status"] == "available" for row in selected),
               "available_message_references": sum(row["message_references"] for row in selected if row["status"] == "available"),
               "statuses": dict(Counter(row["status"] for row in selected)),
               "network_bytes": downloader.used_bytes, "max_file_bytes": downloader.max_file_bytes,
               "max_total_bytes": downloader.max_total_bytes, "request_timeout_seconds": downloader.timeout,
               "https_tun": downloader.https_tun,
               "original_http_tun": downloader.original_http_tun,
               "explicit_protocol_or_encryption_corrections": sum(bool(row.get("correction_applied")) for row in selected),
               "explicit_legacy_emoji_corrections": sum(bool(row.get("legacy_correction_applied")) for row in selected),
               "automatic_failure_retries": 0}
    temporary = downloader.output / "download_summary.json.tmp"
    temporary.write_text(json.dumps(summary, indent=2), encoding="utf8")
    temporary.replace(downloader.output / "download_summary.json")
    print(json.dumps(summary), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", default=str(Path(os.environ.get("UNIFIED_MEDIA_METADATA_DIR") or RUNTIME / "media_metadata") / "sticker.jsonl"))
    parser.add_argument("--resolved", default=str(RUNTIME / "wechat-stickers-resolved.jsonl"))
    parser.add_argument("--output", default=str(RUNTIME / "sticker-download"))
    parser.add_argument("--limit", type=int, help="Maximum new unique hashes this run; previous attempts are retained")
    parser.add_argument("--https-tun", action="store_true", help="For a verified local TUN Fake-IP route: require HTTPS and certificate validation while allowing its benchmark IP ranges")
    parser.add_argument("--original-http-tun", action="store_true", help="On a verified local Meta/TUN route, preserve message-supplied HTTP(S); official domain restrictions and HTTPS certificate verification remain enabled")
    parser.add_argument("--recheck-unrequested", action="store_true", help="Recheck earlier DNS blocks that issued zero HTTP requests after correcting the local network mode")
    parser.add_argument("--correct-protocol-and-encryption", action="store_true", help="One explicit correction for an HTTPS-upgraded original HTTP URL or encrypt_url whose XML key was not previously applied")
    parser.add_argument("--legacy-sticker-cbc-correction", action="store_true", help="One explicit legacy emoji CBC decode after an earlier ECB-only failure; expected XML MD5 remains mandatory")
    options = parser.parse_args()
    if options.limit is not None and options.limit < 1:
        parser.error("limit must be positive")
    run(options)
