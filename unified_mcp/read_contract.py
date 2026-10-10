"""Shared read-result identity and coverage checks, before exposing records."""
from __future__ import annotations


def page_warnings(page):
    value = page.get("warnings", [])
    if not isinstance(value, list):
        value = [value]
    return [item for item in value if item]


def page_source_complete(page):
    for value in (page.get("coverage"), page.get("query", {}).get("coverage")):
        if isinstance(value, dict) and (value.get("source_complete") is False or value.get("complete") is False):
            return False
    # Compatibility with older readers: these warnings affect message coverage,
    # unlike optional OCR/ASR failures which must not hide readable text.
    return not any("fts unavailable" in str(warning).lower() or "main payloads only" in str(warning).lower()
                   for warning in page_warnings(page))


def canonical_chat(source, page, requested, expected=None, *, chat_type=None):
    requested = str(requested)
    canonical = requested
    chat = page.get("chat") or page.get("contact")
    if source == "qq" and isinstance(chat, dict) and chat_type is not None:
        if chat.get("chat_type") is not None and chat["chat_type"] != chat_type:
            raise ValueError("Source returned a different chat type")
    if source == "qq" and chat_type is not None and page.get("query", {}).get("chat_type") is not None:
        if page["query"]["chat_type"] != chat_type:
            raise ValueError("Source returned a different query chat type")
    if source == "qq" and isinstance(chat, dict) and chat.get("identity_verified") is True:
        kind = chat.get("chat_type", "private")
        fields = ("group_id", "group_id_str") if kind == "group" else ("discuss_id", "id") if kind == "discuss" else ("uid", "uin")
        stable = {str(chat[k]) for k in fields if chat.get(k) is not None and str(chat[k])}
        canonical = str(chat.get("canonical_id") or "")
        aliases = chat.get("aliases")
        if (not canonical or canonical not in stable or not isinstance(aliases, list)
                or not all(isinstance(alias, str) and alias in stable for alias in aliases)
                or requested not in aliases):
            raise ValueError("Source returned an unverified chat alias")
    if expected is not None and canonical != expected:
        raise ValueError("Resolved chat identity changed during pagination; restart the query")
    return canonical


def validate_record_chat(source, row, canonical, *, chat_type=None):
    if not isinstance(row, dict):
        raise ValueError("Source returned an invalid record")
    if source == "qq" and chat_type is not None and row.get("chat_type") is not None and row["chat_type"] != chat_type:
        raise ValueError("Source returned a record from another chat type")
    identifiers = [row.get("chat_id")]
    if source == "wechat":
        identifiers.append(row.get("talker"))
        if isinstance(row.get("id"), dict):
            identifiers.append(row["id"].get("talker"))
    if any(str(value) != canonical for value in identifiers if value is not None and value != ""):
        # Never interpolate rejected identities or message text into errors.
        raise ValueError("Source returned a record from another chat")


def validated_page_projection(page):
    """Do not echo arbitrary unvalidated reader envelope fields to a client."""
    query_keys = {"has_more", "returned", "limit", "offset", "next_offset", "next_cursor", "order", "display_order", "canonical_chat_id"}
    coverage_keys = {"complete", "pagination_complete", "source_complete", "identity_complete", "reason", "reasons", "scope"}
    result = {"messages": page["messages"], "query": {key: value for key, value in page.get("query", {}).items() if key in query_keys},
              "warnings": page_warnings(page)}
    if isinstance(page.get("coverage"), dict):
        result["coverage"] = {key: value for key, value in page["coverage"].items() if key in coverage_keys}
    return result
