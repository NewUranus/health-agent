"""统一结构化日志和跨进程 span 上下文；不记录业务正文或异常正文。"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import inspect
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid

correlation = ContextVar("telemetry_correlation", default={})
_runtime = None
ATTRIBUTES = {"name", "kind", "node", "tool", "model", "scene", "status", "status_code", "method", "route",
    "duration_ms", "cached", "attempt", "input_tokens", "output_tokens", "error_type", "reason", "queue_wait_ms",
    "worker_id", "count", "code", "logger", "file", "line", "stack", "template_id", "remote_parent", "release"}


def safe_attributes(values):
    result = {}
    for key, value in values.items():
        if key not in ATTRIBUTES or value is None:
            continue
        if isinstance(value, (bool, int, float)):
            result[key] = value
        elif isinstance(value, str):
            result[key] = value[:512]
    return result


def current():
    return dict(correlation.get())


@contextmanager
def bind(**values):
    token = correlation.set({**current(), **values})
    try:
        yield
    finally:
        correlation.reset(token)


def carrier():
    ctx = current()
    if not ctx.get("trace_id") or not ctx.get("span_id"):
        return {}
    return {"traceparent": f'00-{ctx["trace_id"]}-{ctx["span_id"]}-01',
            "request_id": ctx.get("request_id", ""), "task_id": ctx.get("task_id", "")}


def extract(values):
    if not isinstance(values, dict):
        return {}
    match = re.fullmatch(r"00-([0-9a-f]{32})-([0-9a-f]{16})-(?:00|01)", str(values.get("traceparent", "")))
    if not match or match[1] == "0" * 32 or match[2] == "0" * 16:
        return {}
    ctx = {"trace_id": match[1], "span_id": match[2]}
    for key in ("request_id", "task_id"):
        value = values.get(key, "")
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
            ctx[key] = value
    return ctx


def emit(event, *, level="INFO", message="", **attributes):
    logging.getLogger("healthpilot").log(getattr(logging, level, logging.INFO), event,
        extra={"telemetry_event": event, "telemetry_message": message, "telemetry_attributes": safe_attributes(attributes)})


@contextmanager
def span(name, *, kind="internal", **attributes):
    parent = current()
    ctx = {**parent, "trace_id": parent.get("trace_id") or uuid.uuid4().hex,
           "parent_span_id": parent.get("span_id", ""), "span_id": uuid.uuid4().hex[:16]}
    fields = {"name": name, "kind": kind, **attributes}
    started = time.perf_counter()
    with bind(**ctx):
        emit("span.started", **fields)
        try:
            yield fields
        except BaseException as exc:
            fields.update(status="cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "error", error_type=type(exc).__name__)
            raise
        finally:
            fields.setdefault("status", "ok")
            fields["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
            emit("span.finished", level="ERROR" if fields["status"] == "error" else "INFO", **fields)


def traced(name, *, kind="internal"):
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def asynchronous(*args, **kwargs):
                with span(name, kind=kind):
                    return await function(*args, **kwargs)
            return asynchronous
        @wraps(function)
        def synchronous(*args, **kwargs):
            with span(name, kind=kind):
                return function(*args, **kwargs)
        return synchronous
    return decorate


class StructuredHandler(logging.Handler):
    def __init__(self, service, buffer):
        super().__init__(logging.INFO)
        self.service, self.buffer = service, buffer
        self.instance = uuid.uuid4().hex

    def emit(self, record):
        try:
            ctx = current()
            fields = safe_attributes(getattr(record, "telemetry_attributes", {}))
            fields.update(logger=record.name[:100], file=os.path.basename(record.pathname), line=record.lineno)
            event = getattr(record, "telemetry_event", "library.event")
            message = getattr(record, "telemetry_message", "模块日志，动态正文已省略")
            if not hasattr(record, "telemetry_event"):
                fields["template_id"] = hashlib.sha256(str(record.msg).encode()).hexdigest()[:16]
            if record.exc_info:
                fields["error_type"] = record.exc_info[0].__name__
                fields["stack"] = " > ".join(f'{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}' for frame in traceback.extract_tb(record.exc_info[2])[-8:])[:512]
            item = {"id": uuid.uuid4().hex, "at": int(time.time() * 1000), "level": record.levelname,
                "service": self.service, "instance": self.instance,
                **{key: ctx.get(key, "") for key in ("trace_id", "span_id", "parent_span_id", "request_id", "task_id")},
                "event": event[:100], "message": message[:512], "attributes": fields}
            # MCP 的 stdout 是协议通道，所有结构化日志只能写 stderr。
            try:
                sys.stderr.write(json.dumps(item, ensure_ascii=False) + "\n")
            except (OSError, ValueError):
                pass
            self.buffer.append(item)
        except Exception:
            self.buffer.diagnostic("telemetry.handler_failed", 1)


@contextmanager
def runtime(service):
    global _runtime
    from app.telemetry_store import LogBuffer
    previous = _runtime
    buffer = LogBuffer()
    _runtime = buffer
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    root.handlers = [StructuredHandler(service, buffer)]
    root.setLevel(logging.INFO)
    # 禁止 SDK 默认 handler 绕过脱敏或输出原始 URL/异常正文。
    saved = {}
    names = {name for name, value in logging.Logger.manager.loggerDict.copy().items() if isinstance(value, logging.Logger)}
    names.update({"uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore", "mcp", "healthpilot.access"})
    for name in names:
        logger = logging.getLogger(name)
        saved[name] = (logger.handlers[:], logger.propagate, logger.level)
        logger.handlers, logger.propagate = [], True
        logger.setLevel(logging.WARNING if name in {"httpx", "httpcore", "uvicorn.access"} else logging.INFO)
    try:
        emit("service.started", release=os.getenv("HEALTH_AGENT_RELEASE", "local"))
        yield buffer
    finally:
        emit("service.stopped")
        root.handlers, root.level = old_handlers, old_level
        for name, (handlers, propagate, level) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers, logger.propagate, logger.level = handlers, propagate, level
        buffer.close()
        _runtime = previous


def status():
    return _runtime.status() if _runtime else {"export_error": "not_started"}


class TraceMiddleware:
    """ASGI 层覆盖整个响应流，避免只测到 SSE 响应头的耗时。"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        parent = {}
        if os.getenv("HEALTH_AGENT_TRUST_TRACEPARENT") == "1":
            headers = dict(scope.get("headers", []))
            parent = extract({"traceparent": headers.get(b"traceparent", b"").decode("ascii", errors="ignore")})
        with bind(**parent, request_id=request_id), span("http.request", kind="server", method=scope["method"]) as fields:
            async def traced_send(message):
                if message["type"] == "http.response.start":
                    fields["status_code"] = message["status"]
                    if message["status"] >= 500:
                        fields["status"] = "error"
                    headers = [(key, value) for key, value in message.get("headers", []) if key.lower() not in {b"x-trace-id", b"x-request-id"}]
                    headers.extend([(b"x-trace-id", current()["trace_id"].encode()), (b"x-request-id", request_id.encode())])
                    message = {**message, "headers": headers}
                await send(message)
            try:
                await self.app(scope, receive, traced_send)
            finally:
                fields["route"] = getattr(scope.get("route"), "path", "unmatched")
