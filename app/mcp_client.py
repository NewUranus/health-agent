"""MCP stdio 长连接与进程内 LRU 缓存。"""
import asyncio
import copy
import json
import sys
import os
from app.result_cache import WEB_TOOLS, tool_key
from app.telemetry import span, carrier, emit
from collections import OrderedDict
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parent.parent


class MCPClient:
    def __init__(self, maxsize=256):
        self.maxsize = maxsize
        self.cache = OrderedDict()
        self.results = None
        self.session = None
        self._inflight = {}

    async def __aenter__(self):
        self.stack = AsyncExitStack()
        try:
            params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"], cwd=str(ROOT),
                env={key: value for key, value in os.environ.items() if key.startswith("OTEL_") or key in {"HEALTH_AGENT_RELEASE", "HEALTH_AGENT_TRACE_SAMPLE_RATIO", "HEALTH_AGENT_TELEMETRY_HMAC_KEY"}})
            reader, writer = await self.stack.enter_async_context(stdio_client(params))
            self.session = await self.stack.enter_async_context(ClientSession(reader, writer))
            await self.session.initialize()
        except BaseException:
            await self.stack.aclose()
            self.session = None
            raise
        return self

    async def __aexit__(self, *exc):
        await self.stack.__aexit__(*exc)
        self.session = None

    async def list_tools(self):
        if self.session is None:
            raise RuntimeError("MCP 连接未启动")
        return (await self.session.list_tools()).tools

    async def _count(self, kind, hit):
        if self.results is None:
            return
        try:
            await asyncio.to_thread(self.results.record, kind, hit)
        except Exception as exc:
            emit("cache.unavailable", kind=kind, error_type=type(exc).__name__)

    async def call_with_trace(self, name, args):
        # 网络资料会变化且可能暂时失败，不进入健康计算的永久缓存。
        if name in WEB_TOOLS:
            result = await asyncio.wait_for(self._invoke(name, args), timeout=65 if name == "search_web" else 25)
            return {"tool": name, "args": args, "result": result, "via": "mcp", "cached": False}
        key = tool_key(name, args)
        emit("mcp.cache", tool=name, cached=key in self.cache or key in self._inflight)

        def remember(result):
            self.cache[key] = copy.deepcopy(result)
            if len(self.cache) > self.maxsize:
                self.cache.popitem(last=False)

        if key in self.cache:
            self.cache.move_to_end(key)
            await self._count("tool", True)
            return {"tool": name, "args": args, "result": copy.deepcopy(self.cache[key]), "via": "mcp", "cached": True}
        if key in self._inflight:
            result = copy.deepcopy(await asyncio.shield(self._inflight[key]))
            await self._count("tool", True)
            return {"tool": name, "args": args, "result": result, "via": "mcp", "cached": True}

        async def resolve():
            if self.results is not None:
                try:
                    stored = await asyncio.to_thread(self.results.read, "tool", key)
                except Exception as exc:
                    emit("cache.unavailable", kind="tool", error_type=type(exc).__name__)
                    stored = None
                if stored is not None:
                    return stored, True
            result = await self._invoke(name, args)
            if self.results is not None:
                try:
                    await asyncio.to_thread(self.results.write, "tool", key, result)
                except Exception as exc:
                    emit("cache.unavailable", kind="tool", error_type=type(exc).__name__)
            return result, False

        task = asyncio.create_task(resolve())
        self._inflight[key] = task
        try:
            result, hit = await task
            remember(result)
            await self._count("tool", hit)
        finally:
            self._inflight.pop(key, None)
        return {"tool": name, "args": args, "result": result, "via": "mcp", "cached": hit}

    async def _invoke(self, name, args):
        with span("mcp.call", kind="client", tool=name):
            if self.session is None:
                raise RuntimeError("MCP 连接未启动")
            response = await self.session.call_tool(name, args, meta={"healthpilot": carrier()})
            if response.is_error:
                raise ValueError("工具参数无效，请检查年龄范围、数值与必填字段")
            result = json.loads(next(c.text for c in response.content if c.type == "text"))
            return result["result"] if isinstance(result, dict) and set(result) == {"result"} else result

    async def call_tool(self, name, args):
        return (await self.call_with_trace(name, args))["result"]
