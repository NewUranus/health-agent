"""组装 LangGraph、MCP 与记忆，输出并记录逐节点事件。"""
import asyncio
import hashlib
import json
import os
import re
import time
from contextlib import AsyncExitStack
from pathlib import Path
from weakref import WeakValueDictionary

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from app.graph import OWNERS, build_graph
from app.llm import make_embedder, make_llm
from app.mcp_client import MCPClient
from app.memory import memory_backend
from app.rag import Retriever

ROOT = Path(__file__).resolve().parent.parent


class HealthAgent:
    def __init__(self, mock: bool = False):
        load_dotenv(ROOT / ".env")
        self.mock = mock
        self.mcp = MCPClient()
        self.graph = None
        self._local_memory = None
        self._retriever = None
        self._locks = WeakValueDictionary()

    async def __aenter__(self):
        self.stack = AsyncExitStack()
        try:
            if not os.getenv("DATABASE_URL") and self._local_memory is not None:
                self.checkpointer, self.store = self._local_memory
            else:
                self.checkpointer, self.store = await self.stack.enter_async_context(memory_backend())
                if not os.getenv("DATABASE_URL"):
                    self._local_memory = (self.checkpointer, self.store)
            await self.stack.enter_async_context(self.mcp)
            if self._retriever is None:
                self._retriever = await asyncio.to_thread(Retriever.from_dir, ROOT / "corpus", make_embedder(self.mock))
            self.graph = build_graph(self.mcp, self.store, self._retriever, make_llm(self.mock), self.mock, self.checkpointer)
        except BaseException:
            await self.stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc):
        try:
            await self.stack.__aexit__(*exc)
        finally:
            self.graph = None

    async def stream(self, thread_id: str, message: str, user_id: str | None = None):
        if self.graph is None:
            async with self:
                async for event in self.stream(thread_id, message, user_id):
                    yield event
            return
        user_id = user_id or thread_id
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            async for event in self._stream(thread_id, message, user_id):
                yield event

    async def _stream(self, thread_id, message, user_id):
        trace_name = thread_id if re.fullmatch(r"[\w-]{1,128}", thread_id) else hashlib.sha256(thread_id.encode()).hexdigest()
        path = ROOT / "runs" / "traces" / (trace_name + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = time.perf_counter()
        config = {"configurable": {"thread_id": json.dumps([user_id, thread_id], ensure_ascii=False)}}
        reply = ""
        def record(event):
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(event, ensure_ascii=False) + "\n")
            return event
        try:
            async for namespace, updates in self.graph.astream({"text": message, "user_id": user_id, "messages": [HumanMessage(message)], "reply": ""}, config,
                                                               stream_mode="updates", subgraphs=True):
                for name, update in updates.items():
                    if name not in OWNERS or not isinstance(update, dict):
                        continue
                    now = time.perf_counter()
                    event = {"type": "node", "node": name, "agent": OWNERS[name], "ms": max(0, int((now - previous) * 1000)), "data": update.get("event_data", {})}
                    previous = now
                    if "reply" in update:
                        reply = update["reply"]
                    yield record(event)
            yield record({"type": "message", "content": reply})
        except Exception:
            yield record({"type": "error", "message": "本轮未能完成。请检查输入范围（风险模型适用30–74岁），或稍后重试。"})
        yield record({"type": "done"})
