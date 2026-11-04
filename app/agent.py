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
from langgraph.types import Command
from app.graph import OWNERS, build_graph
from app.llm import make_embedder, make_llm
from app.mcp_client import MCPClient
from app.memory import memory_backend
from app.rag import Retriever
from app.knowledge import MemoryKnowledgeBase, PgKnowledgeBase
from app.http_security import request_id_context
from app.telemetry import span, current, emit
from app.user_lock import user_execution_lock
from app.knowledge_access import knowledge_scope, evidence_refs
from app.model_gateway import model_run, save_usage
from app.task_context import task_execution

ROOT = Path(__file__).resolve().parent.parent


def has_interrupt(snapshot):
    """同时检查主图与嵌套子图的暂停任务。"""
    return bool(getattr(snapshot, "interrupts", ())) or any(
        task.interrupts or has_interrupt(task.state)
        for task in getattr(snapshot, "tasks", ())
    )


class HealthAgent:
    version = "v1"

    def __init__(self, mock: bool = False):
        load_dotenv(ROOT / ".env")
        self.mock = mock
        self.mcp = MCPClient()
        self.graph = None
        self._local_memory = None
        self.kb = None
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
            if self.kb is None:
                url = os.getenv("DATABASE_URL")
                self.kb = PgKnowledgeBase(url, make_embedder(self.mock)) if url else MemoryKnowledgeBase(make_embedder(self.mock))
            self.stack.push_async_callback(self.kb.close)
            await self.kb.setup()
            builtin = await asyncio.to_thread(Retriever.from_dir, ROOT / "corpus")
            await self.kb.seed_builtin(builtin.chunks)
            self.graph = build_graph(self.mcp, self.store, self.kb, make_llm(self.mock), self.mock, self.checkpointer)
        except BaseException:
            await self.stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc):
        try:
            await self.stack.__aexit__(*exc)
        finally:
            self.graph = None

    async def stream(self, thread_id: str, message: str, user_id: str | None = None, *, web_search: bool = False):
        if self.graph is None:
            async with self:
                async for event in self.stream(thread_id, message, user_id, web_search=web_search):
                    yield event
            return
        user_id = user_id or thread_id
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock, user_execution_lock(user_id):
            with span("agent.run"), knowledge_scope(user_id), model_run() as usage:
                try:
                    async for event in self._stream(thread_id, message, user_id, web_search=web_search):
                        yield event
                finally:
                    execution = task_execution.get()
                    await asyncio.to_thread(save_usage, user_id, execution.task_id if execution else None,
                                            request_id_context.get(), usage.records)

    async def _stream(self, thread_id, message, user_id, *, web_search=False):
        trace_name = thread_id if re.fullmatch(r"[\w-]{1,128}", thread_id) else hashlib.sha256(thread_id.encode()).hexdigest()
        trace_dir = ROOT / "runs" / "traces"
        if user_id.startswith("account:"):
            trace_dir = trace_dir / "accounts"
        path = trace_dir / (trace_name + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = time.perf_counter()
        config = {"configurable": {"thread_id": json.dumps([user_id, thread_id], ensure_ascii=False)}}
        reply = ""
        evidence = []
        reply_refs = []
        def record(event):
            with path.open("a", encoding="utf-8") as file:
                # 所有模式均只保留元数据，健康正文和工具参数不落日志。
                logged = {key: value for key, value in event.items() if key in ("type", "node", "agent", "ms")}
                logged.update(current())
                emit("agent.event", node=event.get("node"), status=event.get("type"), duration_ms=event.get("ms"),
                     level="ERROR" if event.get("type") == "error" else "INFO")
                file.write(json.dumps(logged, ensure_ascii=False) + "\n")
            return event
        try:
            snapshot = await self.graph.aget_state(config, subgraphs=True)
            graph_input = Command(resume=message) if has_interrupt(snapshot) else {
                "text": message, "user_id": user_id, "messages": [HumanMessage(message)], "reply": "",
                "web_search": web_search, "web_evidence": [], "web_status": {}}
            question_seen = False
            async for namespace, updates in self.graph.astream(graph_input, config,
                                                               stream_mode="updates", subgraphs=True):
                for name, update in updates.items():
                    if name == "__interrupt__" and update and not question_seen:
                        question = update[0].value
                        question_seen = True
                        reply = question["question"]
                        yield record({"type": "node", "node": "checkin_question", "agent": OWNERS["checkin_question"], "ms": 0,
                                      "data": {key: question[key] for key in ("key", "step", "total")}})
                        continue
                    if name not in OWNERS or not isinstance(update, dict):
                        continue
                    now = time.perf_counter()
                    event = {"type": "node", "node": name, "agent": OWNERS[name], "ms": max(0, int((now - previous) * 1000)), "data": update.get("event_data", {}), "evidence": update.get("reply_evidence", [])}
                    if event["data"].get("chunks"):
                        evidence.extend(event["data"]["chunks"])
                        event["data"] = {**event["data"], "chunks": await self.kb.valid_evidence(event["data"]["chunks"])}
                    if event["evidence"] and not await self.kb.valid_references(event["evidence"]):
                        event["data"] = {"evidence_unavailable": True}
                    previous = now
                    if "reply" in update:
                        reply = update["reply"]
                        reply_refs = update.get("reply_evidence", [])
                    yield record(event)
            if (evidence and len(await self.kb.valid_evidence(evidence)) != len(evidence)) or not await self.kb.valid_references(reply_refs):
                reply = "本轮使用的资料已撤回或权限发生变化，请重新提问获取当前可用依据。"
            yield record({"type": "message", "content": reply, "evidence": reply_refs or evidence_refs(evidence)})
        except Exception:
            yield record({"type": "error", "message": "本轮未能完成。请检查输入范围（风险模型适用30–74岁），或稍后重试。"})
        yield record({"type": "done"})
