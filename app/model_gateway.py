"""模型调用限额、并发保护、短期熔断与最小化用量记录。"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import os
import time
import uuid

scene = ContextVar("model_scene", default="general")
usage = ContextVar("model_usage", default=None)


@dataclass
class Usage:
    calls: int = 0
    records: list = field(default_factory=list)


@contextmanager
def model_run():
    current = Usage()
    token = usage.set(current)
    try:
        yield current
    finally:
        usage.reset(token)


@contextmanager
def model_scene(name):
    token = scene.set(name)
    try:
        yield
    finally:
        scene.reset(token)


class ModelUnavailable(RuntimeError):
    pass


class EvidenceChanged(ModelUnavailable):
    pass


class Control:
    def __init__(self, concurrency):
        self.slots = asyncio.Semaphore(concurrency)
        self.failures = {}
        self.open_until = {}


class ModelGateway:
    def __init__(self, client, model, fallback=None, fallback_model=None, control=None):
        self.client, self.model = client, model
        self.fallback, self.fallback_model = fallback, fallback_model
        self.max_calls = int(os.getenv("HEALTH_AGENT_LLM_MAX_CALLS", "12"))
        self.max_chars = int(os.getenv("HEALTH_AGENT_LLM_MAX_INPUT_CHARS", "60000"))
        concurrency = int(os.getenv("HEALTH_AGENT_LLM_CONCURRENCY", "4"))
        if not 1 <= self.max_calls <= 100 or not 1000 <= self.max_chars <= 500000 or not 1 <= concurrency <= 32:
            raise ValueError("模型调用限额配置超出范围。")
        self.control = control or Control(concurrency)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def bind_tools(self, tools):
        return ModelGateway(self.client.bind_tools(tools), self.model,
            self.fallback.bind_tools(tools) if self.fallback else None, self.fallback_model, self.control)

    async def ainvoke(self, messages):
        run = usage.get()
        if run is None:
            with model_run():
                return await self.ainvoke(messages)
        if sum(len(str(message.content)) for message in messages) > self.max_chars:
            raise ModelUnavailable("模型输入超过本轮允许范围。")
        for client, model in [(self.client, self.model), (self.fallback, self.fallback_model)]:
            if client is None:
                continue
            if self.control.open_until.get(model, 0) > time.monotonic():
                continue
            if run.calls >= self.max_calls:
                raise ModelUnavailable("本轮模型调用次数已达上限。")
            run.calls += 1
            started = time.monotonic()
            record = {"model": model, "scene": scene.get(), "status": "failed", "input_tokens": None, "output_tokens": None}
            acquired = False
            try:
                await asyncio.wait_for(self.control.slots.acquire(), timeout=2)
                acquired = True
                check = permission_check.get()
                if check is not None and not await check():
                    raise EvidenceChanged("资料访问范围已变化，请重新获取依据。")
                async with asyncio.timeout(35):
                    response = await client.ainvoke(messages)
                metadata = getattr(response, "usage_metadata", None) or {}
                record.update(status="succeeded", input_tokens=metadata.get("input_tokens"), output_tokens=metadata.get("output_tokens"))
                self.control.failures[model] = 0
                self.control.open_until.pop(model, None)
                return response
            except EvidenceChanged:
                record["status"] = "access_changed"
                raise
            except asyncio.CancelledError:
                record["status"] = "cancelled"
                raise
            except Exception as exc:
                # 仅记录分类；异常正文可能含提供商请求或密钥，不能落库。
                record["status"] = "busy" if not acquired else "timeout" if isinstance(exc, TimeoutError) else "failed"
                count = self.control.failures.get(model, 0) + 1
                if acquired:
                    self.control.failures[model] = count
                if acquired and count >= 3:
                    self.control.open_until[model] = time.monotonic() + 30
            finally:
                if acquired:
                    self.control.slots.release()
                record["duration_ms"] = int((time.monotonic() - started) * 1000)
                run.records.append(record)
        raise ModelUnavailable("模型暂不可用，已转入确定性降级流程。")


def save_usage(user_id, task_id, request_id, records):
    if not records or not user_id.startswith("account:"):
        return
    from app.auth import AuthStore
    database = AuthStore()
    with database.connection() as execute:
        for record in records:
            execute("INSERT INTO hp_model_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, user_id[8:], task_id, request_id, int(time.time()), record["scene"], record["model"],
                 record["status"], record["input_tokens"], record["output_tokens"], record["duration_ms"]))


permission_check = ContextVar("model_permission_check", default=None)


@contextmanager
def model_evidence(kb, chunks, history=()):
    async def check():
        valid = await kb.valid_evidence(chunks)
        previous = await kb.visible_history(history)
        return len(valid) == len(chunks) and len(previous) == len(history)
    token = permission_check.set(check if hasattr(kb, "valid_evidence") else None)
    try:
        yield
    finally:
        permission_check.reset(token)
