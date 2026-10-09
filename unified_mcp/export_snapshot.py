"""Export locally available records with explicit pagination and coverage evidence.

Media is inventoried here, not decoded or semantically read. Each source page is
saved before advancing. Existing output files are never overwritten.
"""

import argparse
import asyncio
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path

from unified_mcp.server import Gateway
from unified_mcp.timeline import decoded_result, normal_message, TZ


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def export(options):
    output = Path(options.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "coverage.json"
    if manifest_path.exists() or any(output.glob("*.jsonl")):
        raise FileExistsError("Choose a new output directory; existing exports are preserved.")
    snapshot = int(dt.datetime.now(TZ).timestamp())
    report = {
        "started_at": dt.datetime.now(TZ).isoformat(), "before_epoch_exclusive": snapshot,
        "complete": False, "sources": {},
        "coverage": "Locally synchronized records only; not an immutable database snapshot.",
        "media_content_read": False,
        "media_note": "WeChat media decoding is disabled for this inventory. QQ media hints are metadata, not decoded content.",
    }
    write_json(manifest_path, report)
    gateway = Gateway()
    try:
        for source, chat in (("wechat", options.wechat), ("qq", options.qq)):
            if not chat:
                continue
            state = {"chat_id": chat, "complete": False, "pages": 0, "records": 0,
                     "duplicate_records": 0, "message_id_collisions": 0, "offset": 0, "warnings": []}
            report["sources"][source] = state
            seen, message_ids, kinds, directions = set(), set(), Counter(), Counter()
            last_timestamp = None
            path = output / (source + ".jsonl")
            with path.open("x", encoding="utf-8") as stream:
                while True:
                    args = {"limit": options.page_size, "offset": state["offset"], "order": "asc",
                            "display_order": "asc", "before": str(snapshot)}
                    if source == "wechat":
                        args.update(talker=chat, include_media_paths=False, include_images=False)
                    else:
                        args.update(contact=chat, chat_type="private", include_media=True)
                    page = decoded_result(await gateway.fetch(source, "chat_timeline", args))
                    if page.get("errors") or page.get("error"):
                        raise RuntimeError(f"{source} returned errors: {page.get('errors') or page.get('error')}")
                    rows = page.get("messages")
                    query = page.get("query", {})
                    if not isinstance(rows, list) or type(query.get("has_more")) is not bool:
                        raise RuntimeError(f"{source} omitted messages or terminal pagination metadata")
                    for row in rows:
                        if row.get("error"):
                            raise RuntimeError(f"{source} returned an unreadable row")
                        msg = normal_message(source, row, chat)
                        if not msg["message_id"] or msg["timestamp"] is None:
                            raise RuntimeError(f"{source} returned a row without identity/time")
                        # Local message IDs can repeat across database shards.
                        # Only identical source records are safe to collapse.
                        identity = hashlib.sha256(json.dumps(row, ensure_ascii=False,
                                                              sort_keys=True).encode()).hexdigest()
                        if identity in seen:
                            state["duplicate_records"] += 1
                            continue
                        if msg["message_id"] in message_ids:
                            state["message_id_collisions"] += 1
                        if last_timestamp is not None and msg["timestamp"] < last_timestamp:
                            raise RuntimeError(f"{source} page order moved backwards")
                        last_timestamp = msg["timestamp"]
                        seen.add(identity)
                        message_ids.add(msg["message_id"])
                        stream.write(json.dumps(msg, ensure_ascii=False) + "\n")
                        state["records"] += 1
                        kinds[msg.get("kind") or "unknown"] += 1
                        directions[msg["direction"]] += 1
                        state.setdefault("first_time", msg["time"])
                        state["last_time"] = msg["time"]
                    stream.flush()
                    state["pages"] += 1
                    state["last_query"] = query
                    state["kinds"] = dict(kinds)
                    state["directions"] = dict(directions)
                    for warning in page.get("warnings", []):
                        if warning not in state["warnings"]:
                            state["warnings"].append(warning)
                    if query["has_more"] is False:
                        state["complete"] = True
                        write_json(manifest_path, report)
                        print(f"{source}: complete, {state['records']} records / {state['pages']} pages", flush=True)
                        break
                    next_offset = query.get("next_offset")
                    if not rows or type(next_offset) is not int or next_offset <= state["offset"]:
                        raise RuntimeError(f"{source} pagination stopped advancing")
                    state["offset"] = next_offset
                    write_json(manifest_path, report)
                    if state["pages"] == 1 or state["pages"] % 10 == 0:
                        print(f"{source}: page {state['pages']}, {state['records']} records", flush=True)
        # The per-source evidence remains intact alongside this convenience merge.
        merged = []
        for source in report["sources"]:
            with (output / (source + ".jsonl")).open(encoding="utf-8") as stream:
                merged.extend(json.loads(line) for line in stream)
        merged.sort(key=lambda row: (row["timestamp"], row["source"]))
        with (output / "merged.jsonl").open("x", encoding="utf-8") as stream:
            for row in merged:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        report["complete"] = all(s["complete"] for s in report["sources"].values())
        report["total_records"] = len(merged)
        report["finished_at"] = dt.datetime.now(TZ).isoformat()
        write_json(manifest_path, report)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        write_json(manifest_path, report)
        raise
    finally:
        await gateway.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wechat")
    parser.add_argument("--qq")
    parser.add_argument("--output", required=True)
    parser.add_argument("--page-size", type=int, default=5000)
    options = parser.parse_args()
    if not options.wechat and not options.qq:
        parser.error("Specify at least one resolved stable chat ID")
    if not 1 <= options.page_size <= 5000:
        parser.error("page-size must be between 1 and 5000")
    asyncio.run(export(options))
