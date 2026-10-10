"""One explicit time grammar for native QQ and unified local-history reads."""
from __future__ import annotations

import datetime as dt
import re

TZ = dt.timezone(dt.timedelta(hours=8))
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INTEGER = re.compile(r"^[+-]?\d+$")
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?$")


def parse_time_bound(value, *, before=False):
    if value is None:
        return None
    if type(value) not in (str, int):
        raise ValueError("Time bounds must be ISO dates/timestamps or integer Unix seconds/milliseconds")
    raw = str(value).strip()
    if not raw:
        raise ValueError("An explicit time bound cannot be empty")
    if INTEGER.fullmatch(raw):
        digits = raw.lstrip("+-")
        if len(digits) == 8:
            raise ValueError("Ambiguous eight-digit time; use YYYY-MM-DD or an explicit ISO timestamp")
        if len(digits) > 13:
            raise ValueError("Unix time must be seconds or 13-digit milliseconds")
        number = int(raw)
        return number // 1000 if len(digits) == 13 else number
    if DAY.fullmatch(raw):
        parsed = dt.datetime.combine(dt.date.fromisoformat(raw), dt.time(), TZ)
        if before:
            parsed += dt.timedelta(days=1)
    else:
        # Do not accept fromisoformat's compact or week-date extensions here.
        if not ISO.fullmatch(raw):
            raise ValueError("Use YYYY-MM-DD or an ISO timestamp with hours and minutes")
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.microsecond:
            raise ValueError("Time bounds require whole seconds; fractional seconds would be silently rounded")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
    return int(parsed.timestamp())


def validate_time_range(after, before, *, end_date_inclusive=True):
    start = parse_time_bound(after)
    end = parse_time_bound(before, before=end_date_inclusive)
    if start is not None and end is not None and start > end:
        raise ValueError("after must not be later than before")
    return start, end


def date_scope(args):
    result = dict(args)
    if "date" in result:
        value = result.pop("date")
        if not isinstance(value, str) or not DAY.fullmatch(value):
            raise ValueError("date must be a nonempty YYYY-MM-DD string")
        if any(result.get(key) is not None for key in ("after", "before")):
            raise ValueError("date cannot be combined with after/before")
        dt.date.fromisoformat(value)
        result["after"] = value
        result["before"] = value
    validate_time_range(result.get("after"), result.get("before"))
    return result


def message_timestamp(value):
    if type(value) is not int or value < 0:
        raise ValueError("Source timestamp must be nonnegative integer Unix seconds")
    return value
