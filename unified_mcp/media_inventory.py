"""Export exact-chat raw media metadata, with agent pagination evidence.

The full interface returns a bare list. Each page is therefore cross-checked
against the same query's agent envelope before its pagination cursor advances.
No media decryption, ASR, message sending, or source database changes occur.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path

from unified_mcp.backend import WeChatBackend


def unpack(result):
    if result.isError:
        raise RuntimeError("WeChat metadata query returned an MCP error")
    blocks = [b.text for b in result.content if b.type == "text"]
    if len(blocks) != 1:
        raise RuntimeError("WeChat metadata response must contain one JSON block")
    data = json.loads(blocks[0])
    if isinstance(data, dict) and (data.get("error") or data.get("errors")):
        raise RuntimeError("WeChat metadata query returned an error envelope")
    return data


def identity(row):
    nested = row.get("id") or row
    # Agent output omits a zero server ID; full/resources render it as "0".
    # The local ID, timestamp and kind still identify that unsent record.
    return (str(nested.get("local_id")), str(nested.get("server_id_str") or "0"),
            row.get("create_time"), row.get("kind_name") or row.get("kind"))


def save_manifest(path, report):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    temporary.replace(path)


async def export(options):
    output = Path(options.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "manifest.json"
    resume = getattr(options, "resume", False)
    if resume:
        report = json.loads(manifest.read_text(encoding="utf8"))
        if any(report.get(key) != value for key, value in {
                "talker": options.talker, "before_epoch_exclusive": options.before,
                "page_size": options.page_size}.items()) or set(report["kinds"]) - set(options.kinds):
            raise ValueError("Resume scope must exactly match the saved metadata query")
        # A saved page and its manifest must agree before appending anything.
        for kind, state in report["kinds"].items():
            path = output / f"{kind}.jsonl"
            count = 0
            if path.exists():
                with path.open(encoding="utf8") as source:
                    for line in source:
                        row = json.loads(line)
                        if row.get("talker") != options.talker or row.get("kind_name") != kind:
                            raise ValueError("Saved metadata contains an out-of-scope record")
                        count += 1
            if count != state["records"]:
                raise ValueError("Saved metadata count does not match its manifest")
        previous_error = report.pop("error", None)
        if previous_error:
            report.setdefault("resumed_attempts", []).append({
                "previous_error": previous_error, "resumed_at": dt.datetime.now().astimezone().isoformat()})
    else:
        if manifest.exists() or any((output / f"{kind}.jsonl").exists() for kind in options.kinds):
            raise FileExistsError("Choose a fresh metadata output directory")
        report = {"talker": options.talker, "before_epoch_exclusive": options.before,
                  "page_size": options.page_size, "complete": False, "kinds": {},
                  "scope": "Locally synchronized records at a fixed time upper bound; not a DB snapshot",
                  "include_media_paths": False, "started_at": dt.datetime.now().astimezone().isoformat()}
    save_manifest(manifest, report)
    backend = WeChatBackend(timeout_seconds=180)
    try:
        for kind in options.kinds:
            state = report["kinds"].setdefault(kind, {"complete": False, "records": 0, "pages": []})
            if state["complete"]:
                continue
            offset = state["pages"][-1]["query"].get("next_offset") if state["pages"] else 0
            if type(offset) is not int or offset < 0:
                raise ValueError(f"Saved pagination cursor is invalid for {kind}")
            with (output / f"{kind}.jsonl").open("a" if resume else "x", encoding="utf8") as stream:
                while True:
                    args = {"talker": options.talker, "type": kind, "order": "asc",
                            "display_order": "asc", "limit": options.page_size, "offset": offset,
                            "before": str(options.before), "include_media_paths": False}
                    rows = unpack(await backend.call("messages", {**args, "fields": "full"}))
                    if not isinstance(rows, list) or any(row.get("error") for row in rows):
                        raise RuntimeError(f"Invalid raw metadata page for {kind}")
                    envelope = unpack(await backend.call("messages", {**args, "view": "agent"}))
                    query = envelope.get("query", {}) if isinstance(envelope, dict) else {}
                    agent_rows = envelope.get("messages", []) if isinstance(envelope, dict) else []
                    if type(query.get("has_more")) is not bool:
                        raise RuntimeError(f"Missing pagination evidence for {kind}")
                    if [identity(row) for row in rows] != [identity(row) for row in agent_rows]:
                        raise RuntimeError(f"Full/agent record identity mismatch for {kind}")
                    if any(row.get("talker") != options.talker or row.get("kind_name") != kind for row in rows):
                        raise RuntimeError(f"Out-of-scope raw media row for {kind}")
                    for row in rows:
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    stream.flush()
                    state["records"] += len(rows)
                    state["pages"].append({"offset": offset, "returned": len(rows), "query": query,
                                           "full_agent_identity_match": True})
                    if rows:
                        state.setdefault("first_time", rows[0]["create_time_human"])
                        state["last_time"] = rows[-1]["create_time_human"]
                    if not query["has_more"]:
                        state["complete"] = True
                    save_manifest(manifest, report)
                    print(f"{kind}: {state['records']} records, complete={state['complete']}", flush=True)
                    if state["complete"]:
                        break
                    following = query.get("next_offset")
                    if not rows or type(following) is not int or following <= offset:
                        raise RuntimeError(f"Non-advancing metadata pagination for {kind}")
                    offset = following
        report["complete"] = all(state["complete"] for state in report["kinds"].values())
        report["finished_at"] = dt.datetime.now().astimezone().isoformat()
        save_manifest(manifest, report)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        save_manifest(manifest, report)
        raise
    finally:
        await backend.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--talker", required=True)
    parser.add_argument("--before", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--kinds", nargs="+", default=["voice", "sticker", "image", "video"])
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--resume", action="store_true", help="Resume a matching incomplete export after validating saved page counts")
    options = parser.parse_args()
    if not 1 <= options.page_size <= 5000:
        parser.error("page-size must be between 1 and 5000")
    asyncio.run(export(options))
