"""HTTP 身份边界、统一错误和请求审计，不参与健康判断。"""
import asyncio
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

from app.auth import AuthFailure, AuthStore, COOKIE_NAME, SESSION_SECONDS
from app.knowledge_access import principal

request_id_context = ContextVar("request_id", default="")
logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserInfo(BaseModel):
    id: str
    username: str


class SessionInfo(BaseModel):
    required: bool
    user: UserInfo | None
    expires_at: int | None = None


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
            personal = resource in {"/chat", "/history", "/followup", "/auth/me", "/tasks"} or resource.startswith(("/tools/", "/tasks/"))
            for method, operation in methods.items():
                if method not in {"get", "post", "patch", "delete", "put"}:
                    continue
                if admin:
                    operation["security"] = [{"KnowledgeAdmin": []}]
                elif personal and application.state.auth_required:
                    operation["security"] = [{"UserSession": []}]
                if application.state.auth_required and not admin and method != "get":
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
        resource = resource_path(request.url.path)
        request.state.request_id = uuid.uuid4().hex
        request.state.user = None
        context = request_id_context.set(request.state.request_id)
        store = application.state.auth_store
        required = application.state.auth_required
        admin = bool(resource and (resource == "/admin" or resource.startswith("/admin/")))
        personal = bool(resource and (resource in {"/chat", "/history", "/followup", "/tasks"} or resource.startswith(("/tools/", "/tasks/"))))
        audited = required and bool(admin or personal or (resource and resource.startswith("/auth/")))
        actor = None
        knowledge_token = principal.set("")
        try:
            if required:
                request.state.user = await asyncio.to_thread(store.resolve, request.cookies.get(COOKIE_NAME))
                actor = request.state.user["id"] if request.state.user else None
            if audited:
                # 不保存查询参数、正文、密码、Cookie 或健康资料。
                await asyncio.to_thread(store.audit, actor, "http.requested", resource, request.state.request_id, 0)
            if admin:
                password = os.getenv("ADMIN_PASSWORD")
                authorization = request.headers.get("authorization", "")
                if required and not password:
                    raise AuthFailure(503, "ADMIN_DISABLED", "后台尚未配置管理员凭证。")
                if password and not hmac.compare_digest(authorization.encode(), ("Bearer " + password).encode()):
                    raise AuthFailure(401, "ADMIN_AUTH_REQUIRED", "密码错误")
                actor = "knowledge-admin"
                principal.set(None)
            if required and personal:
                if not request.state.user:
                    raise AuthFailure(401, "AUTH_REQUIRED", "请先登录。")
                expected = request.headers.get("x-healthpilot-user")
                if expected and expected != request.state.user["id"]:
                    raise AuthFailure(403, "SESSION_CHANGED", "登录账号已改变，请重新登录。")
            if required and not admin and resource and request.method not in {"GET", "HEAD", "OPTIONS"}:
                # 自定义头阻止跨站表单提交；不开放跨域携带凭证。
                origin = request.headers.get("origin")
                if (request.headers.get("x-healthpilot-request") != "1"
                        or request.headers.get("sec-fetch-site") == "cross-site"
                        or (origin and (urlsplit(origin).scheme, urlsplit(origin).netloc) != (request.url.scheme, request.url.netloc))):
                    raise AuthFailure(403, "CSRF_REJECTED", "请求来源校验失败，请从本站页面重试。")
            response = await call_next(request)
        except AuthFailure as exc:
            response = error_response(request, exc.status, exc.message, exc.code)
        except Exception as exc:
            logger.error("请求失败 request_id=%s type=%s", request.state.request_id, type(exc).__name__)
            response = error_response(request, 500, "服务暂时无法完成请求，请联系管理员。")
        finally:
            request_id_context.reset(context)
            principal.reset(knowledge_token)
        if audited:
            try:
                await asyncio.to_thread(store.audit, actor, "http.response", resource, request.state.request_id, response.status_code)
            except Exception:
                # 已执行的操作不可隐瞒；记录审计故障并停止返回业务数据。
                logger.error("审计写入失败 request_id=%s", request.state.request_id)
                response = error_response(request, 503, "审计服务暂不可用，请联系管理员核对操作结果。", "AUDIT_UNAVAILABLE")
        response.headers["X-Request-ID"] = request.state.request_id
        if resource:
            response.headers.update(version_headers)
            response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/auth/me", response_model=SessionInfo, summary="查看当前登录状态")
    async def me(request: Request):
        if not application.state.auth_required:
            return {"required": False, "user": None}
        if not request.state.user:
            raise AuthFailure(401, "AUTH_REQUIRED", "请先登录。")
        return {"required": True, "user": {k: request.state.user[k] for k in ("id", "username")},
                "expires_at": request.state.user["expires_at"]}

    @router.post("/auth/login", response_model=SessionInfo, summary="登录并签发可撤销会话")
    async def login(request: Request, body: LoginRequest):
        if not application.state.auth_required:
            raise AuthFailure(409, "AUTH_DEMO_MODE", "当前为免登录演示模式。")
        # 限制同时执行的密码哈希数量，避免登录突发耗尽进程内存。
        async with application.state.login_slots:
            token, user, expires = await asyncio.to_thread(application.state.auth_store.login,
                body.username, body.password, request.client.host if request.client else "unknown",
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
