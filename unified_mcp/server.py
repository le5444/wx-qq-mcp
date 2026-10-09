"""One MCP endpoint for existing WeChat tools, QQ and combined timelines."""

from __future__ import annotations

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import io
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from unified_mcp.backend import WeChatBackend
from unified_mcp.timeline import decoded_result, merged_timeline
from unified_mcp.media_validation import sanitize_media_payload
from unified_mcp.voice_transcription import VoiceService
from unified_mcp.image_text import ImageTextReader, image_reference_rank

BASE = Path(__file__).resolve().parent


def text_result(data, error=False):
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False, default=str))], isError=error)


class Gateway:
    def __init__(self):
        self.wechat = WeChatBackend(
            command=os.environ.get("UNIFIED_WECHAT_COMMAND"),
            args=json.loads(os.environ.get("UNIFIED_WECHAT_ARGS", "[]")),
            idle_seconds=float(os.environ.get("UNIFIED_IDLE_SECONDS", "60")),
        )
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qq-reader")
        self.qq = None
        self.voice = VoiceService()
        self.image_text = None
        self.wechat_tools = [types.Tool.model_validate(t) for t in json.loads((BASE / "wechat_legacy_tools.json").read_text(encoding="utf-8"))]

    def _qq_call(self, name, args):
        if self.qq is None:
            from unified_mcp import qq_adapter
            self.qq = qq_adapter
        return self.qq.call(name, args)

    async def fetch(self, source, name, args):
        args = dict(args)
        include_image_text = args.pop("include_image_text", True)
        if source == "wechat":
            result = await self.wechat.call(name, args)
            if (result.isError or args.get("include_media_paths") is False or args.get("include_images") is False
                    or (name == "media_resources" and args.get("include_local_paths") is False)):
                return result
            content = []
            for block in result.content:
                if block.type != "text":
                    content.append(block)
                    continue
                try:
                    payload = json.loads(block.text)
                except (ValueError, TypeError):
                    content.append(block)
                    continue
                checked = await self._optional_enrichment(payload, name, args, include_image_text)
                content.append(block.model_copy(update={"text": json.dumps(checked, ensure_ascii=False)}))
            updates = {"content": content}
            if result.structuredContent is not None:
                updates["structuredContent"] = await self._optional_enrichment(result.structuredContent, name, args, include_image_text)
            return result.model_copy(update=updates)
        result = await asyncio.get_running_loop().run_in_executor(self.executor, self._qq_call, name, args)
        if include_image_text and args.get("include_media", True):
            try:
                return await self._image_text_payload(result)
            except Exception as exc:
                return self._enrichment_warning(result, type(exc).__name__)
        return result

    @staticmethod
    def _enrichment_warning(payload, error_type):
        if isinstance(payload, list):
            return [Gateway._enrichment_warning(item, error_type) for item in payload]
        if not isinstance(payload, dict):
            return payload
        result = dict(payload)
        previous = result.get("warnings", [])
        result["warnings"] = (previous if isinstance(previous, list) else [previous]) + [
            "media_enrichment_failed: " + error_type + "; original messages preserved; media completeness not verified"]
        return result

    async def _optional_enrichment(self, payload, name, args, include_image_text):
        checked = payload
        try:
            checked = await self._enrich_wechat(payload, name, args)
            if include_image_text:
                checked = await self._image_text_payload(checked)
            return checked
        except Exception as exc:
            checked = await asyncio.to_thread(sanitize_media_payload, checked)
            return self._enrichment_warning(checked, type(exc).__name__)

    @staticmethod
    def _media_nodes(payload):
        found = []
        def visit(value):
            if isinstance(value, dict):
                if value.get("kind", value.get("kind_name")) in {"image", "sticker", "emoji", "voice", "video"}:
                    found.append(value)
                else:
                    for child in value.values():
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(payload)
        return found

    @staticmethod
    def _json_result(result):
        if result.isError:
            raise RuntimeError("Media metadata query failed")
        text = [block.text for block in result.content if block.type == "text"]
        return json.loads(text[0]) if len(text) == 1 else None

    async def _enrich_wechat(self, payload, name, args):
        nodes = self._media_nodes(payload)
        if any(row.get("kind", row.get("kind_name")) in {"image", "sticker", "emoji"} for row in nodes):
            from unified_mcp.wechat_media import enrich_wechat_media
            payload = await asyncio.to_thread(enrich_wechat_media, payload)
            unresolved = [row for row in self._media_nodes(payload)
                          if row.get("wechat_media_resolution", {}).get("status") == "metadata_missing"
                          or (row.get("wechat_media_resolution", {}).get("status") == "unavailable"
                              and row.get("wechat_media_resolution", {}).get("local_candidates") == 0)]
            if unresolved and name in {"messages", "chat_timeline"}:
                try:
                    allowed = {"chat", "talker", "after", "before", "keyword", "type", "kind_name",
                               "base_kind", "limit", "offset", "order", "display_order", "sender"}
                    query = {k: v for k, v in args.items() if k in allowed}
                    query.update(fields="full", include_media_paths=False)
                    metadata = self._json_result(await self.wechat.call("messages", query))
                    resources = []
                    times = [row.get("create_time") for row in unresolved if row.get("create_time") is not None]
                    if times:
                        resource_query = {k: args[k] for k in ("talker", "chat") if k in args}
                        resource_query.update(after=str(min(times)), before=str(max(times)+1),
                                              limit=5000, include_local_paths=False, include_debug=True)
                        resources = self._json_result(await self.wechat.call("media_resources", resource_query))
                    payload = await asyncio.to_thread(enrich_wechat_media, payload,
                                                       metadata if isinstance(metadata, list) else [],
                                                       resources if isinstance(resources, list) else [])
                except Exception:
                    # Keep the already-readable messages and explicit unavailable
                    # media statuses when the optional metadata query fails.
                    if isinstance(payload, dict):
                        payload.setdefault("warnings", []).append("media_metadata_refresh_failed; unresolved media was not marked readable")
        if any(row.get("kind", row.get("kind_name")) == "voice" for row in nodes):
            payload = await self.voice.enrich(payload, transcribe=os.environ.get("WX_UNIFIED_VOICE_CACHE_ONLY") != "1")
        if any(row.get("kind", row.get("kind_name")) == "video" for row in nodes):
            from unified_mcp.video_media import enrich_wechat_video
            payload = await asyncio.to_thread(enrich_wechat_video, payload)
            video_nodes = [row for row in self._media_nodes(payload)
                           if row.get("wechat_video_resolution", {}).get("status") == "metadata_missing"]
            if video_nodes:
                try:
                    resources = []
                    for row in video_nodes:
                        ident = row.get("id", {})
                        talker = row.get("talker") or ident.get("talker") or args.get("talker")
                        server_id = row.get("server_id_str") or ident.get("server_id_str")
                        local_id = row.get("local_id") or ident.get("local_id")
                        if not talker or (not server_id and local_id is None):
                            continue
                        query = {"talker": talker, "limit": 20, "include_local_paths": False, "include_debug": True}
                        if server_id:
                            query["server_id_str"] = str(server_id)
                        if local_id is not None:
                            query["local_id"] = int(local_id)
                        data = self._json_result(await self.wechat.call("media_resources", query))
                        if isinstance(data, list):
                            resources.extend(data)
                    payload = await asyncio.to_thread(enrich_wechat_video, payload, resources)
                except Exception:
                    if isinstance(payload, dict):
                        payload.setdefault("warnings", []).append("video_resource_refresh_failed; video availability remains unverified")
        return await asyncio.to_thread(sanitize_media_payload, payload)

    async def _image_text_payload(self, payload):
        if isinstance(payload, list):
            return [await self._image_text_payload(item) for item in payload]
        if not isinstance(payload, dict):
            return payload
        result = dict(payload)
        for key, value in payload.items():
            if key == "images" and isinstance(value, list):
                references = []
                best = None
                if payload.get("kind", payload.get("kind_name")) in {"image", "sticker", "emoji"} and value:
                    best = max(range(len(value)), key=lambda index: image_reference_rank(value[index]))
                for index, reference in enumerate(value):
                    if isinstance(reference, dict) and reference.get("path"):
                        if best is not None and index != best:
                            references.append({**reference, "ocr": {"status": "not_requested", "reason": "largest_available_variant_processed"}})
                            continue
                        if self.image_text is None:
                            self.image_text = ImageTextReader()
                        ocr = await asyncio.to_thread(self.image_text.read, reference["path"])
                        references.append({**reference, "ocr": ocr})
                    else:
                        references.append(reference)
                result[key] = references
            elif isinstance(value, (dict, list)):
                result[key] = await self._image_text_payload(value)
        return result

    def tools(self):
        # Importing the original definitions does not open databases or recover keys.
        import qq_mcp_server
        tools = list(self.wechat_tools)
        for tool in tools:
            if tool.name in {"messages", "chat_timeline", "media_resources"}:
                tool = tool.inputSchema.setdefault("properties", {})
                tool["include_image_text"] = {"type": "boolean", "default": True,
                    "description": "Read local image text with Windows OCR; preserves image paths for visual inspection."}
        for original in qq_mcp_server.TOOLS:
            definition = {**original, "name": "qq_" + original["name"]}
            if original["name"] in {"messages", "chat_timeline"}:
                import copy
                definition["inputSchema"] = copy.deepcopy(original["inputSchema"])
                definition["inputSchema"].setdefault("properties", {})["include_image_text"] = {"type": "boolean", "default": True}
            definition["description"] = "[QQ] " + original["description"]
            tools.append(types.Tool.model_validate(definition))
        properties = {
            "wechat_chat": {"type": "string", "description": "Resolved WeChat username/talker"},
            "qq_chat": {"type": "string", "description": "Resolved QQ contact UID/UIN or group ID"},
            "qq_chat_type": {"type": "string", "enum": ["private", "group", "discuss"], "default": "private"},
            "after": {"type": "string"}, "before": {"type": "string"}, "keyword": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
            "cursor": {"type": "string"}, "include_media": {"type": "boolean", "default": True},
            "include_image_text": {"type": "boolean", "default": True},
        }
        for name in ("unified_timeline", "unified_search"):
            schema = {"type": "object", "properties": properties, "additionalProperties": False,
                      "anyOf": [{"required": ["wechat_chat"]}, {"required": ["qq_chat"]}]}
            if name == "unified_search":
                schema["required"] = ["keyword"]
            tools.append(types.Tool(name=name, description="Read WeChat and/or QQ together. Resolve IDs first; confirm both IDs refer to the intended person. Preserve source, sender and original media. Page with next_cursor; partial is NOT no messages.", inputSchema=schema))
        tools.append(types.Tool(name="unified_sources", description="Diagnose local WeChat/QQ readiness and lazy-process state; never returns keys.", inputSchema={"type": "object", "properties": {}, "additionalProperties": False}))
        tools.append(types.Tool(name="unified_resolve_chat", description="Find candidates in both platforms; never automatically equate people by nickname.", inputSchema={"type": "object", "properties": {"query": {"type": "string"}, "qq_chat_type": {"enum": ["private", "group"], "type": "string"}}, "required": ["query"], "additionalProperties": False}))
        tools.append(types.Tool(name="unified_read_image", description="Read a local WeChat/QQ image or sticker: returns local OCR text and an image preview for visual understanding. Animated previews show the first frame only; original path is preserved.", inputSchema={"type": "object", "properties": {"path": {"type": "string"}, "include_image": {"type": "boolean", "default": True}}, "required": ["path"], "additionalProperties": False}))
        return tools

    async def call(self, name, args):
        try:
            if name == "unified_read_image":
                if self.image_text is None:
                    self.image_text = ImageTextReader()
                result = await asyncio.to_thread(self.image_text.read, args["path"])
                response = text_result({"path": args["path"], "ocr": result}, error=result.get("status") == "unavailable")
                if args.get("include_image", True) and result.get("status") in {"ok", "no_text"}:
                    from PIL import Image
                    with Image.open(args["path"]) as source_image:
                        preview = source_image.convert("RGB")
                        preview.thumbnail((1600, 1600))
                        buffer = io.BytesIO()
                        preview.save(buffer, format="JPEG", quality=90)
                    response.content.append(types.ImageContent(type="image", mimeType="image/jpeg", data=base64.b64encode(buffer.getvalue()).decode()))
                return response
            if name == "unified_sources":
                try:
                    qq = await self.fetch("qq", "diagnose", {})
                except Exception as exc:
                    qq = {"ready": False, "error": str(exc)}
                return text_result({"wechat": self.wechat.status(), "qq": qq,
                                    "mode": "local_database_readers", "original_clients_modified": False})
            if name == "unified_resolve_chat":
                result = {}
                for source in ("wechat", "qq"):
                    method = "resolve_chat" if source == "wechat" else "resolve_group" if args.get("qq_chat_type") == "group" else "resolve_contact"
                    try:
                        result[source] = decoded_result(await self.fetch(source, method, {"query": args["query"]}))
                    except Exception as exc:
                        result[source] = {"error": str(exc)}
                return text_result(result, error=any(v.get("error") for v in result.values()))
            if name in {"unified_timeline", "unified_search"}:
                if name == "unified_search" and not args.get("keyword", "").strip():
                    raise ValueError("Search requires a nonempty keyword")
                result = await merged_timeline(args, self.fetch)
                return text_result(result, error=result["status"] != "ok")
            if name.startswith("qq_"):
                return text_result(await self.fetch("qq", name[3:], args))
            if name not in {t.name for t in self.wechat_tools}:
                raise ValueError("Unknown tool")
            return await self.fetch("wechat", name, args)
        except Exception as exc:
            return text_result({"error": str(exc), "tool": name}, error=True)

    async def close(self):
        try:
            await self.wechat.close()
        finally:
            await self.voice.close()
            if self.image_text is not None:
                await asyncio.to_thread(self.image_text.close)
        def cleanup():
            if self.qq and self.qq.legacy._VFS_CONNECTION:
                self.qq.legacy._VFS_CONNECTION.close()
                self.qq.legacy._VFS_CONNECTION = None
        await asyncio.get_running_loop().run_in_executor(self.executor, cleanup)
        self.executor.shutdown(wait=True)


async def serve():
    gateway = Gateway()
    @asynccontextmanager
    async def lifespan(_):
        try:
            yield {}
        finally:
            await gateway.close()
    server = Server("wx-mcp-unified", version="0.2.1", lifespan=lifespan,
                    instructions="wx-mcp now exposes BOTH WeChat and QQ. Original unprefixed tools are WeChat; qq_* tools are QQ; unified_* read both. Do not infer senders or assume matching nicknames are the same person. Page until has_more is false. Report errors, unavailable media and unsynchronized history explicitly. No message-sending tools are provided.")
    @server.list_tools()
    async def list_tools():
        return gateway.tools()
    @server.call_tool()
    async def call_tool(name, arguments):
        return await gateway.call(name, arguments)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


async def cli_call(name, args):
    gateway = Gateway()
    try:
        result = await gateway.call(name, args)
        for block in result.content:
            if block.type == "text":
                print(block.text)
        return 1 if result.isError else 0
    finally:
        await gateway.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--call")
    parser.add_argument("--args", default="{}")
    options = parser.parse_args()
    if options.call:
        return asyncio.run(cli_call(options.call, json.loads(options.args)))
    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
