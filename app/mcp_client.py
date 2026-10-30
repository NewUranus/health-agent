"""MCP stdio 长连接与进程内 LRU 缓存。"""
import asyncio
import copy
import json
import sys
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
        self.session = None
        self._inflight = {}

    async def __aenter__(self):
        self.stack = AsyncExitStack()
        try:
            params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"], cwd=str(ROOT))
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

    async def call_with_trace(self, name, args):
        key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False, allow_nan=False))
        cached = key in self.cache or key in self._inflight
        if key in self.cache:
            self.cache.move_to_end(key)
            result = copy.deepcopy(self.cache[key])
        elif key in self._inflight:
            result = copy.deepcopy(await asyncio.shield(self._inflight[key]))
        else:
            task = asyncio.create_task(self._invoke(name, args))
            self._inflight[key] = task
            try:
                result = await task
                self.cache[key] = copy.deepcopy(result)
                if len(self.cache) > self.maxsize:
                    self.cache.popitem(last=False)
            finally:
                self._inflight.pop(key, None)
        return {"tool": name, "args": args, "result": result, "via": "mcp", "cached": cached}

    async def _invoke(self, name, args):
        if self.session is None:
            raise RuntimeError("MCP 连接未启动")
        response = await self.session.call_tool(name, args)
        if response.is_error:
            raise ValueError("工具参数无效，请检查年龄范围、数值与必填字段")
        result = json.loads(next(c.text for c in response.content if c.type == "text"))
        return result["result"] if isinstance(result, dict) and set(result) == {"result"} else result

    async def call_tool(self, name, args):
        return (await self.call_with_trace(name, args))["result"]
