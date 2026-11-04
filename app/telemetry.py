"""OpenTelemetry 链路与 OTLP 日志；所有导出经过元数据白名单。"""
import asyncio
from contextlib import contextmanager
from functools import wraps
import hashlib
import hmac
import inspect
import json
import logging
import math
import os
import re
import sys
import time
import traceback
import uuid

import structlog
from structlog.contextvars import bind_contextvars, get_contextvars, reset_contextvars
from opentelemetry import context as otel_context, trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.trace import SpanKind, Status, StatusCode, Link
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor, ReadableSpan
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ParentBased, TraceIdRatioBased
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

_runtime = None
_propagator = TraceContextTextMapPropagator()
BUSINESS_KEYS = ("request_id", "task_id", "thread_id", "user_hash")
ATTRIBUTES = {"name", "kind", "node", "tool", "model", "scene", "status", "status_code", "method", "route",
    "duration_ms", "cached", "attempt", "input_tokens", "output_tokens", "error_type", "reason", "queue_wait_ms",
    "worker_id", "count", "code", "logger", "file", "line", "stack", "template_id", "release"}
AUTO_ATTRIBUTES = {"http.request.method", "http.response.status_code", "http.method", "http.status_code", "db.system", "db.system.name", "db.operation", "db.operation.name"}
_SPAN_NAMES = {"http.request", "queue.submit", "worker.execute", "agent.run", "agent.node", "model.attempt", "knowledge.search", "mcp.call", "mcp.execute"}
_renderer = structlog.processors.JSONRenderer(ensure_ascii=False)


def safe_attributes(values):
    return {key: value[:512] if isinstance(value, str) else value for key, value in values.items()
            if key in ATTRIBUTES | AUTO_ATTRIBUTES | set(BUSINESS_KEYS) and value is not None
            and isinstance(value, (str, bool, int, float)) and not (isinstance(value, float) and not math.isfinite(value))}


def opaque_id(value, domain="user"):
    """不使用可枚举的明文哈希；未配置密钥时不采集用户/会话标识。"""
    key = os.getenv("HEALTH_AGENT_TELEMETRY_HMAC_KEY", "")
    if not value or len(key) < 32:
        return ""
    return hmac.new(key.encode(), (domain + ":" + str(value)).encode(), hashlib.sha256).hexdigest()[:32]


def current():
    values = {key: get_contextvars().get(key, "") for key in BUSINESS_KEYS}
    active = trace.get_current_span()
    ctx = active.get_span_context()
    parent = getattr(active, "parent", None)
    return {**values, "trace_id": f"{ctx.trace_id:032x}" if ctx.is_valid else "",
            "span_id": f"{ctx.span_id:016x}" if ctx.is_valid else "",
            "parent_span_id": f"{parent.span_id:016x}" if parent and parent.is_valid else ""}


@contextmanager
def bind(**values):
    tokens = bind_contextvars(**{key: value for key, value in values.items() if key in BUSINESS_KEYS})
    remote = values.get("_otel_context")
    token = otel_context.attach(remote) if remote is not None else None
    try:
        yield
    finally:
        if token is not None:
            otel_context.detach(token)
        reset_contextvars(**tokens)


def carrier():
    result = {}
    _propagator.inject(result)
    result.update({key: value for key, value in current().items() if key in BUSINESS_KEYS and value})
    return result


def extract(values):
    if not isinstance(values, dict):
        return {}
    # 只接收 traceparent；不传播来自外部的 baggage 或任意业务字段。
    parent = _propagator.extract({"traceparent": str(values.get("traceparent", ""))}, context=otel_context.Context())
    if not trace.get_current_span(parent).get_span_context().is_valid:
        return {}
    result = {"_otel_context": parent}
    for key in BUSINESS_KEYS:
        value = values.get(key, "")
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
            result[key] = value
    return result


def emit(event, *, level="INFO", message="", **attributes):
    logging.getLogger("healthpilot").log(getattr(logging, level, logging.INFO), event,
        extra={"telemetry_event": event, "telemetry_message": message, "telemetry_attributes": safe_attributes(attributes)})


@contextmanager
def span(name, *, kind="internal", **attributes):
    tracer = _runtime.tracer if _runtime else trace.get_tracer("healthpilot")
    fields = {"name": name, "kind": kind, **attributes}
    started = time.perf_counter()
    # 禁止 SDK 自动记录异常正文与堆栈，由下面的分类信息替代。
    with tracer.start_as_current_span(name, kind=getattr(SpanKind, kind.upper(), SpanKind.INTERNAL),
            record_exception=False, set_status_on_exception=False) as active:
        active.set_attributes(safe_attributes({**fields, **get_contextvars()}))
        emit("span.started", **fields)
        try:
            yield fields
        except BaseException as exc:
            fields.update(status="cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "error", error_type=type(exc).__name__)
            raise
        finally:
            fields.setdefault("status", "ok")
            fields["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
            active.set_attributes(safe_attributes(fields))
            active.set_status(Status(StatusCode.ERROR if fields["status"] == "error" else StatusCode.OK))
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


class PrivacySpanProcessor(SpanProcessor):
    """自动埋点的 URL、SQL、异常与事件在进入导出队列前删除。"""
    def __init__(self, downstream, resource):
        self.downstream, self.resource = downstream, resource

    def on_start(self, span, parent_context=None):
        span.set_attributes({key: value for key, value in current().items() if key in BUSINESS_KEYS and value})

    def on_end(self, span):
        scope = span.instrumentation_scope
        source = scope.name if scope else ""
        name = span.name if source == "healthpilot" and span.name in _SPAN_NAMES else (
            "http.client" if "httpx" in source else "database.query" if "psycopg" in source else "dependency.call")
        # 构造只含白名单的只读快照，避免将自动埋点的原始属性交给导出器。
        safe = ReadableSpan(name=name, context=span.context, parent=span.parent, resource=self.resource,
            attributes=safe_attributes(span.attributes or {}), events=(),
            links=tuple(Link(link.context) for link in span.links), kind=span.kind,
            status=Status(span.status.status_code), start_time=span.start_time, end_time=span.end_time,
            instrumentation_scope=scope)
        self.downstream.on_end(safe)

    def shutdown(self):
        self.downstream.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.downstream.force_flush(timeout_millis)


class ObservedExporter:
    """记录 OTLP 导出结果；重试与批处理使用官方 SDK。"""
    def __init__(self, exporter):
        self.exporter = exporter
        self.last_success = None
        self.failed_batches = 0
        self.failed_records = 0
        self.error = None

    def export(self, batch):
        try:
            result = self.exporter.export(batch)
            if result.name == "SUCCESS":
                self.last_success, self.error = int(time.time() * 1000), None
            else:
                self.failed_batches += 1
                self.failed_records += len(batch)
                self.error = "otlp_export_failed"
            return result
        except Exception:
            self.failed_batches += 1
            self.failed_records += len(batch)
            self.error = "otlp_export_failed"
            raise

    def shutdown(self):
        self.exporter.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.exporter.force_flush(timeout_millis)


class StructuredHandler(logging.Handler):
    def __init__(self, owner):
        super().__init__(logging.INFO)
        self.owner = owner

    def emit(self, record):
        try:
            fields = safe_attributes(getattr(record, "telemetry_attributes", {}))
            fields.update(logger=record.name[:100], file=os.path.basename(record.pathname), line=record.lineno)
            event = getattr(record, "telemetry_event", "library.event")
            message = getattr(record, "telemetry_message", "模块日志，动态正文已省略")
            if not hasattr(record, "telemetry_event"):
                fields["template_id"] = hashlib.sha256(str(record.msg).encode()).hexdigest()[:16]
            if record.exc_info:
                fields["error_type"] = record.exc_info[0].__name__
                fields["stack"] = " > ".join(f'{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}' for frame in traceback.extract_tb(record.exc_info[2])[-8:])[:512]
            item = {"id": uuid.uuid4().hex, "at": int(record.created * 1000), "level": record.levelname,
                "service": self.owner.service, "instance": self.owner.instance, "release": self.owner.release,
                **current(), "event": event[:100], "message": message[:512], "attributes": fields}
            rendered = _renderer(None, record.levelname.lower(), item)
            # MCP stdout 专用于协议；采集器只接收已经脱敏的日志。
            try:
                sys.stderr.write(rendered + "\n")
            except (OSError, ValueError):
                pass
            # 导出器自己的失败日志不能再次入队，避免故障期间无限递归。
            if not record.name.startswith(("opentelemetry", "urllib3", "requests")):
                severity = SeverityNumber.ERROR if record.levelno >= 40 else SeverityNumber.WARN if record.levelno >= 30 else SeverityNumber.INFO
                self.owner.logger.emit(timestamp=int(record.created * 1000000000), body=rendered,
                    severity_number=severity, severity_text=record.levelname,
                    attributes={"event": item["event"], **{key: item[key] for key in BUSINESS_KEYS if item[key]}})
        except Exception:
            # 故障信息也不能包含原始日志、连接凭证或异常正文。
            try:
                os.write(2, b'{"level":"ERROR","event":"telemetry.handler_failed"}\n')
            except OSError:
                pass


class TelemetryRuntime:
    def __init__(self, service):
        self.service, self.instance = service, uuid.uuid4().hex
        self.release = os.getenv("HEALTH_AGENT_RELEASE", "local")[:200]
        resource = Resource({"service.name": "healthpilot-" + service, "service.namespace": "healthpilot",
            "service.instance.id": self.instance, "service.version": self.release})
        disabled = os.getenv("OTEL_SDK_DISABLED", "false").lower() == "true"
        ratio = float(os.getenv("HEALTH_AGENT_TRACE_SAMPLE_RATIO", "1"))
        if not 0 <= ratio <= 1:
            raise ValueError("链路采样率必须在 0 到 1 之间。")
        self.provider = TracerProvider(resource=resource, sampler=ALWAYS_OFF if disabled else ParentBased(TraceIdRatioBased(ratio)), shutdown_on_exit=False)
        self.logs = LoggerProvider(resource=resource, shutdown_on_exit=False)
        self.exporters = {}
        base = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
        if os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf") != "http/protobuf":
            raise ValueError("本项目 OTLP 导出使用 http/protobuf。")
        for signal, factory in (("traces", OTLPSpanExporter), ("logs", OTLPLogExporter)):
            endpoint = os.getenv("OTEL_EXPORTER_OTLP_" + signal.upper() + "_ENDPOINT") or (base + "/v1/" + signal if base else "")
            if disabled or not endpoint:
                continue
            exporter = ObservedExporter(factory(endpoint=endpoint, timeout=5))
            self.exporters[signal] = exporter
            if signal == "traces":
                batch = BatchSpanProcessor(exporter, max_queue_size=2048, max_export_batch_size=256, schedule_delay_millis=1000)
                self.provider.add_span_processor(PrivacySpanProcessor(batch, resource))
            else:
                self.logs.add_log_record_processor(BatchLogRecordProcessor(exporter, max_queue_size=2048, max_export_batch_size=256, schedule_delay_millis=1000))
        self.tracer = self.provider.get_tracer("healthpilot")
        self.logger = self.logs.get_logger("healthpilot")
        self.instruments = []
        if not disabled:
            for instrument in (HTTPXClientInstrumentor(), PsycopgInstrumentor()):
                if not instrument.is_instrumented_by_opentelemetry:
                    instrument.instrument(tracer_provider=self.provider, capture_parameters=False, enable_commenter=False)
                    self.instruments.append(instrument)

    def close(self):
        for instrument in reversed(self.instruments):
            instrument.uninstrument()
        self.provider.shutdown()
        self.logs.shutdown()

    def status(self):
        return {"engine": "opentelemetry", "central_backend": "loki", "configured": sorted(self.exporters),
                "export_error": next((exporter.error for exporter in self.exporters.values() if exporter.error), None),
                "signals": {key: {"last_success": value.last_success, "failed_batches": value.failed_batches,
                    "failed_records": value.failed_records, "error": value.error} for key, value in self.exporters.items()}}


@contextmanager
def runtime(service):
    global _runtime
    previous = _runtime
    owner = TelemetryRuntime(service)
    _runtime = owner
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    root.handlers = [StructuredHandler(owner)]
    root.setLevel(logging.INFO)
    saved = {}
    names = {name for name, value in logging.Logger.manager.loggerDict.copy().items() if isinstance(value, logging.Logger)}
    names.update({"uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore", "mcp"})
    for name in names:
        logger = logging.getLogger(name)
        saved[name] = (logger.handlers[:], logger.propagate, logger.level)
        logger.handlers, logger.propagate = [], True
        logger.setLevel(logging.WARNING if name in {"httpx", "httpcore", "uvicorn.access"} else logging.INFO)
    try:
        emit("service.started")
        yield owner
    finally:
        emit("service.stopped")
        # 导出器关闭期间仍使用安全 handler，错误只写 stderr。
        owner.close()
        root.handlers, root.level = old_handlers, old_level
        for name, (handlers, propagate, level) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers, logger.propagate, logger.level = handlers, propagate, level
        _runtime = previous


def status():
    return _runtime.status() if _runtime else {"engine": "opentelemetry", "configured": [], "signals": {}, "export_error": "not_started"}


class TraceMiddleware:
    """使用 OTel 服务端 span 覆盖整个 ASGI/SSE 生命周期，不采集 URL/正文。"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        parent = {"_otel_context": otel_context.Context()}
        if os.getenv("HEALTH_AGENT_TRUST_TRACEPARENT") == "1":
            headers = dict(scope.get("headers", []))
            parent.update(extract({"traceparent": headers.get(b"traceparent", b"").decode("ascii", errors="ignore")}))
        with bind(**parent, request_id=request_id, task_id="", thread_id="", user_hash=""), span("http.request", kind="server", method=scope["method"]) as fields:
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
