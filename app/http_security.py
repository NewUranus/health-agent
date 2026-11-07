"""HTTP 身份边界、统一错误和请求审计，不参与健康判断。"""
import asyncio
import time
import json
from contextvars import ContextVar
import hmac
import logging
import os
import uuid
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from typing import Literal

from app.auth import AuthFailure, AuthStore, COOKIE_NAME, GUEST_ROLE, SESSION_SECONDS, role_allows_admin, ADMIN_ROLES
from app import guests
from app.knowledge_access import principal
from app.telemetry import emit, opaque_id
from structlog.contextvars import bind_contextvars, reset_contextvars

request_id_context = ContextVar("request_id", default="")
logger = logging.getLogger(__name__)

RoleName = Literal["user", "knowledge_admin", "ops_admin"]
SessionRole = Literal["user", "knowledge_admin", "ops_admin", "guest"]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=12, max_length=256)
    role: RoleName = "user"


class AccountPasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=12, max_length=256)


class AccountRoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: RoleName


class UserInfo(BaseModel):
    id: str
    username: str
    role: SessionRole = "user"


class GuestQuota(BaseModel):
    limit: int
    remaining: int
    hours: int
    daily_available: bool


class SessionInfo(BaseModel):
    required: bool
    user: UserInfo | None
    expires_at: int | None = None
    guest: GuestQuota | None = None


class LogoutInfo(BaseModel):
    ok: bool


class ErrorInfo(BaseModel):
    detail: str
    code: str
    request_id: str
    retryable: bool
    next_action: str


def auth_required(mock):
    mode = os.getenv("HEALTH_AGENT_AUTH_MODE", "demo" if mock else "required")
    if mode not in {"demo", "required"} or (mode == "demo" and not mock):
        raise ValueError("HEALTH_AGENT_AUTH_MODE 只支持 required 或 demo；demo 只允许用于 Mock 演示。")
    return mode == "required"


def error_response(request, status, detail, code=None, headers=None):
    codes = {400: "INVALID_ARGUMENT", 401: "AUTH_REQUIRED", 403: "ACCESS_DENIED", 404: "NOT_FOUND",
             405: "METHOD_NOT_ALLOWED", 409: "CONFLICT", 413: "PAYLOAD_TOO_LARGE", 422: "INVALID_ARGUMENT",
             429: "RATE_LIMITED", 500: "INTERNAL_ERROR", 502: "UPSTREAM_FAILURE", 503: "SERVICE_UNAVAILABLE"}
    retryable = status in {429, 502, 503, 504}
    action = "稍后重试；写操作重试前请先确认结果。" if retryable else "检查输入或联系管理员，并提供请求编号。"
    if status == 401:
        action = "请登录或重新登录。"
    if status == 403:
        action = "请使用有权限的账号，或刷新页面后重试。"
    if status == 404:
        action = "检查资源是否存在及是否属于当前账号。"
    content = ErrorInfo(detail=detail, code=code or codes.get(status, "REQUEST_FAILED"),
                        request_id=getattr(request.state, "request_id", ""), retryable=retryable, next_action=action)
    return JSONResponse(status_code=status, content=content.model_dump(), headers=headers)


def current_identity(request, supplied=None, fallback=None):
    """登录模式仅认可服务端身份，旧客户端仍可携带与当前用户一致的 ID。"""
    if request.app.state.auth_required:
        user = request.state.user
        if not user:
            raise AuthFailure(401, "AUTH_REQUIRED", "请先登录。")
        if supplied is not None and supplied != user["id"]:
            raise AuthFailure(403, "USER_SCOPE_MISMATCH", "无权访问其他用户的数据。")
        return "account:" + user["id"]
    value = supplied or fallback
    if not value:
        raise HTTPException(422, "请提供用户标识。")
    # 即使切换至演示模式，也不能通过猜测正式用户 ID 读取账号数据。
    return "demo:" + value


def attach_security(application, router, resource_path, version_headers):
    original_openapi = application.openapi

    def openapi():
        schema = original_openapi()
        schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schemes["UserSession"] = {"type": "apiKey", "in": "cookie", "name": COOKIE_NAME,
                                  "description": "通过登录接口获取的 HttpOnly 会话 Cookie。"}
        schemes["KnowledgeAdmin"] = {"type": "http", "scheme": "bearer", "description": "独立的知识库管理员凭证。"}
        for path, methods in schema["paths"].items():
            resource = resource_path(path)
            if not resource:
                continue
            admin = resource.startswith("/admin/")
            personal = resource in {"/chat", "/history", "/followup", "/auth/me", "/tasks"} or resource.startswith(("/tools/", "/tasks/", "/conversations/"))
            for method, operation in methods.items():
                if method not in {"get", "post", "patch", "delete", "put"}:
                    continue
                if admin:
                    operation["security"] = [{"KnowledgeAdmin": []}, {"UserSession": []}]
                elif personal and application.state.auth_required:
                    operation["security"] = [{"UserSession": []}]
                if application.state.auth_required and method != "get":
                    parameters = operation.setdefault("parameters", [])
                    if not any(p.get("name") == "X-HealthPilot-Request" for p in parameters):
                        parameters.append({"name": "X-HealthPilot-Request", "in": "header", "required": True,
                                           "schema": {"type": "string", "enum": ["1"]}, "description": "本站写请求校验头。"})
        return schema

    application.openapi = openapi

    @application.exception_handler(AuthFailure)
    async def auth_error(request, exc):
        return error_response(request, exc.status, exc.message, exc.code)

    @application.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return error_response(request, exc.status_code, str(exc.detail), headers=exc.headers)

    @application.middleware("http")
    async def boundary(request: Request, call_next):
        started = time.perf_counter()
        resource = resource_path(request.url.path)
        request.state.request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
        request.state.user = None
        request.state.admin_auth = None
        context = request_id_context.set(request.state.request_id)
        store = application.state.auth_store
        required = application.state.auth_required
        admin = bool(resource and (resource == "/admin" or resource.startswith("/admin/")))
        personal = bool(resource and (resource in {"/chat", "/history", "/followup", "/tasks", "/conversations"} or resource.startswith(("/tools/", "/tasks/", "/conversations/"))))
        audited = required and bool(admin or personal or (resource and resource.startswith("/auth/")))
        actor = None
        knowledge_token = principal.set("")
        log_tokens = bind_contextvars(user_hash="")
        try:
            if required:
                request.state.user = await asyncio.to_thread(store.resolve, request.cookies.get(COOKIE_NAME))
                actor = request.state.user["id"] if request.state.user else None
                bind_contextvars(user_hash=opaque_id(actor))
            if audited:
                # 不保存查询参数、正文、密码、Cookie 或健康资料。
                await asyncio.to_thread(store.audit, actor, "http.requested", resource, request.state.request_id, 0)
            if admin:
                password = os.getenv("ADMIN_PASSWORD")
                authorization = request.headers.get("authorization", "")
                bearer_ok = bool(password) and hmac.compare_digest(
                    authorization.encode(), ("Bearer " + password).encode())
                role = request.state.user["role"] if request.state.user else None
                session_ok = bool(required and role and role_allows_admin(role, resource))
                if session_ok:
                    request.state.admin_auth = "session"
                    actor = request.state.user["id"]
                    principal.set(None)
                elif bearer_ok:
                    request.state.admin_auth = "bearer"
                    actor = "knowledge-admin"
                    principal.set(None)
                elif required and request.state.user and role in ADMIN_ROLES:
                    raise AuthFailure(403, "ADMIN_ROLE_DENIED", "当前管理员角色无权访问该后台接口。")
                elif required and request.state.user:
                    raise AuthFailure(403, "ACCESS_DENIED", "普通账号不能访问管理后台。")
                elif required and not password:
                    raise AuthFailure(503, "ADMIN_DISABLED", "后台尚未配置管理员凭证。")
                elif password:
                    raise AuthFailure(401, "ADMIN_AUTH_REQUIRED", "密码错误")
                elif required:
                    raise AuthFailure(401, "ADMIN_AUTH_REQUIRED", "请先登录管理账号或提供管理员凭证。")
                else:
                    # 演示模式且未配置共享口令时保持开放后台。
                    request.state.admin_auth = "open"
                    actor = "knowledge-admin"
                    principal.set(None)
            if required and request.state.user and request.state.user["role"] == GUEST_ROLE and guests.guest_blocked(resource):
                raise guests.login_required("该功能")
            if required and personal:
                if not request.state.user:
                    raise AuthFailure(401, "AUTH_REQUIRED", "请先登录。")
                expected = request.headers.get("x-healthpilot-user")
                if expected and expected != request.state.user["id"]:
                    raise AuthFailure(403, "SESSION_CHANGED", "登录账号已改变，请重新登录。")
            cookie_write = required and resource and request.method not in {"GET", "HEAD", "OPTIONS"}
            if cookie_write and (not admin or request.state.admin_auth == "session"):
                # 自定义头阻止跨站表单提交；不开放跨域携带凭证。Bearer 紧急口令不走 Cookie，免 CSRF 头。
                origin = request.headers.get("origin")
                if (request.headers.get("x-healthpilot-request") != "1"
                        or request.headers.get("sec-fetch-site") == "cross-site"
                        or (origin and (urlsplit(origin).scheme, urlsplit(origin).netloc) != (request.url.scheme, request.url.netloc))):
                    raise AuthFailure(403, "CSRF_REJECTED", "请求来源校验失败，请从本站页面重试。")
            response = await call_next(request)
        except AuthFailure as exc:
            response = error_response(request, exc.status, exc.message, exc.code)
        except Exception as exc:
            emit("http.failed", level="ERROR", error_type=type(exc).__name__)
            response = error_response(request, 500, "服务暂时无法完成请求，请联系管理员。")
        finally:
            request_id_context.reset(context)
            principal.reset(knowledge_token)
            reset_contextvars(**log_tokens)
        if audited:
            try:
                await asyncio.to_thread(store.audit, actor, "http.response", resource, request.state.request_id, response.status_code)
            except Exception:
                # 已执行的操作不可隐瞒；记录审计故障并停止返回业务数据。
                emit("audit.unavailable", level="ERROR")
                response = error_response(request, 503, "审计服务暂不可用，请联系管理员核对操作结果。", "AUDIT_UNAVAILABLE")
        response.headers["X-Request-ID"] = request.state.request_id
        if resource:
            response.headers.update(version_headers)
            response.headers["Cache-Control"] = "no-store"
        emit("http.response.headers", route=getattr(request.scope.get("route"), "path", "unmatched"), method=request.method,
             status_code=response.status_code, duration_ms=round((time.perf_counter() - started) * 1000))
        return response

    @router.get("/auth/me", response_model=SessionInfo, summary="查看当前登录状态")
    async def me(request: Request):
        if not application.state.auth_required:
            return {"required": False, "user": None}
        if not request.state.user:
            # 告诉首页可以直接进入免登录体验；名额用完或关闭时只显示登录页
            available = await asyncio.to_thread(guests.availability, application.state.auth_store)
            return error_response(request, 401, "请先登录。", "AUTH_REQUIRED",
                                  headers={"X-HealthPilot-Guest": "available"} if available else None)
        body = {"required": True, "user": {k: request.state.user[k] for k in ("id", "username", "role")},
                "expires_at": request.state.user["expires_at"]}
        if request.state.user["role"] == GUEST_ROLE:
            body["guest"] = await asyncio.to_thread(guests.quota, application.state.auth_store, request.state.user["id"])
        return body

    @router.post("/auth/guest", response_model=SessionInfo, status_code=201, summary="开启免登录体验，签发临时游客会话")
    async def guest(request: Request):
        if not application.state.auth_required:
            raise AuthFailure(409, "AUTH_DEMO_MODE", "当前为免登录演示模式。")
        current = request.state.user
        if current and current["role"] != GUEST_ROLE:
            raise AuthFailure(409, "ALREADY_SIGNED_IN", "已经登录账号，无需免登录体验。")
        store = application.state.auth_store
        if current:
            body = {"required": True, "user": {k: current[k] for k in ("id", "username", "role")}, "expires_at": current["expires_at"],
                    "guest": await asyncio.to_thread(guests.quota, store, current["id"])}
            return JSONResponse(SessionInfo(**body).model_dump(), status_code=200)
        token, user, expires = await asyncio.to_thread(guests.create, store, guests.client_ip(request),
                                                       request.cookies.get(COOKIE_NAME), request.state.request_id)
        body = SessionInfo(required=True, user=UserInfo(**user), expires_at=expires,
                           guest=await asyncio.to_thread(guests.quota, store, user["id"]))
        response = JSONResponse(body.model_dump(), status_code=201)
        response.set_cookie(COOKIE_NAME, token, max_age=expires - int(time.time()), httponly=True,
                            secure=os.getenv("HEALTH_AGENT_COOKIE_SECURE", "1") != "0", samesite="strict", path="/")
        return response

    @router.post("/auth/login", response_model=SessionInfo, summary="登录并签发可撤销会话")
    async def login(request: Request, body: LoginRequest):
        if not application.state.auth_required:
            raise AuthFailure(409, "AUTH_DEMO_MODE", "当前为免登录演示模式。")
        # 限制同时执行的密码哈希数量，避免登录突发耗尽进程内存。
        async with application.state.login_slots:
            token, user, expires = await asyncio.to_thread(application.state.auth_store.login,
                body.username, body.password, guests.client_ip(request),
                request.cookies.get(COOKIE_NAME), request.state.request_id)
        response = JSONResponse(SessionInfo(required=True, user=UserInfo(**user), expires_at=expires).model_dump())
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=os.getenv("HEALTH_AGENT_COOKIE_SECURE", "1") != "0", samesite="strict", path="/")
        return response

    @router.post("/auth/logout", response_model=LogoutInfo, summary="注销当前登录会话")
    async def logout(request: Request):
        if application.state.auth_required:
            await asyncio.to_thread(application.state.auth_store.logout, request.cookies.get(COOKIE_NAME),
                                    request.state.user["id"] if request.state.user else None, request.state.request_id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True,
                               secure=os.getenv("HEALTH_AGENT_COOKIE_SECURE", "1") != "0", samesite="strict")
        return response

    @router.post("/auth/password", summary="修改当前账号密码，并注销其他会话")
    async def change_password(request: Request, body: PasswordChange):
        if not application.state.auth_required:
            raise AuthFailure(409, "AUTH_DEMO_MODE", "当前为免登录演示模式。")
        if not request.state.user:
            raise AuthFailure(401, "AUTH_REQUIRED", "请先登录。")
        async with application.state.login_slots:
            await asyncio.to_thread(application.state.auth_store.change_password, request.state.user["id"],
                                    body.current_password, body.new_password, request.cookies.get(COOKIE_NAME), request.state.request_id)
        return {"ok": True}
