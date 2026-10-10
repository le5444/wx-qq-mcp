"""Conservative ordered parsing of locally observed QQ payload fields.

Only field 45101 is treated as message text. Other printable byte strings are
hints, never recovered original text. Unknown structure remains explicitly
partial. This is an independent parser, not a complete QQ protocol decoder.
"""

from __future__ import annotations

import hashlib
import re


def parse_segments(blob, *, max_fields=2048, max_depth=8):
    data = bytes(blob) if isinstance(blob, (bytes, bytearray, memoryview)) else b""
    state = {"fields": 0, "unknown": 0, "malformed": False, "truncated": False}
    segments = []

    def varint(raw, pos):
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(raw):
                raise ValueError("truncated varint")
            byte = raw[pos]
            pos += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value, pos
        raise ValueError("oversize varint")

    def fields(raw):
        pos = 0
        while pos < len(raw):
            key, pos = varint(raw, pos)
            field, wire = key >> 3, key & 7
            if field == 0:
                raise ValueError("invalid field")
            if wire == 0:
                value, pos = varint(raw, pos)
            else:
                if wire == 2:
                    size, pos = varint(raw, pos)
                elif wire in (1, 5):
                    size = 8 if wire == 1 else 4
                else:
                    raise ValueError("unsupported wire type")
                if size > len(raw) - pos:
                    raise ValueError("truncated value")
                value = raw[pos:pos + size]
                pos += size
            yield field, wire, value

    def descend(raw, path, depth):
        try:
            for field, wire, value in fields(raw):
                state["fields"] += 1
                if state["fields"] > max_fields:
                    state["truncated"] = True
                    return
                field_path = [*path, field]
                if wire != 2:
                    state["unknown"] += 1
                    continue
                try:
                    text = value.decode("utf-8", errors="strict")
                    printable = bool(text) and all(c.isprintable() or c in "\n\r\t" for c in text)
                except UnicodeDecodeError:
                    text, printable = "", False
                if field == 45101 and printable:
                    segments.append({"type": "text", "text": text, "field_path": field_path,
                                     "source": "payload.45101"})
                    continue
                if printable:
                    state["unknown"] += 1
                    if re.fullmatch(r"[a-fA-F0-9]{16,64}\.(?:jpg|jpeg|png|gif|webp)", text, re.I):
                        segments.append({"type": "image_reference_hint", "value": text,
                                         "field_path": field_path, "verified": False})
                    continue
                if not value:
                    state["unknown"] += 1
                    continue
                if depth >= max_depth:
                    state["truncated"] = True
                    continue
                # Validate the nested wire shape before accepting it as a container.
                # Opaque bytes are unknown, not necessarily a malformed message.
                try:
                    count = 0
                    for _ in fields(value):
                        count += 1
                        if count > max_fields:
                            state["truncated"] = True
                            break
                except ValueError:
                    state["unknown"] += 1
                    continue
                descend(value, field_path, depth + 1)
        except ValueError:
            state["malformed"] = True

    if data:
        descend(data, [], 0)
    return {"segments": segments,
            "payload_parse": {"status": "partial" if data else "absent",
                              "known_text_fields": sum(s["type"] == "text" for s in segments),
                              "unknown_fields": state["unknown"], "malformed": state["malformed"],
                              "truncated": state["truncated"], "bytes": len(data),
                              "sha256": hashlib.sha256(data).hexdigest() if data else None,
                              "note": "Ordered known fields only; unknown/reply/forward structure is not fully decoded."}}
