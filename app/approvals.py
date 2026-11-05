"""知识变更单级确认：内容绑定、有效期、幂等决定与执行审计。"""
import asyncio
import hmac
import json
import os
import time
import uuid
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth import AuthFailure, digest
from app.idempotency import idempotent
from app.database import schema_lock_sync
from app.knowledge_review import preview, apply_review


class AccessPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visibility: Literal["shared", "restricted"]
    allowed_user_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_accounts(self):
        import re
        if any(not re.fullmatch(r"[a-f0-9]{32}", item) for item in self.allowed_user_ids):
            raise ValueError("账号标识格式无效。")
        if self.visibility == "shared" and self.allowed_user_ids:
            raise ValueError("共享资料不能附带指定账号。")
        self.allowed_user_ids = sorted(set(self.allowed_user_ids))
        return self


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: int = Field(gt=0)
    action: Literal["publish", "delete", "withdraw", "access"]
    access: AccessPolicy | None = None

    @model_validator(mode="after")
    def validate_action(self):
        if (self.action == "access") != (self.access is not None):
            raise ValueError("权限调整必须且只能携带 access 参数。")
        return self


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class ApprovalSummary(BaseModel):
    document_id: int
    title: str
    version: str
    chunk_count: int
    action: Literal["publish", "delete", "withdraw", "access"]
    replaced: list[dict]
    risk: str
    access: AccessPolicy | None = None


class ApprovalInfo(BaseModel):
    id: str
    action: Literal["publish", "delete", "withdraw", "access"]
    document_id: int
    fingerprint: str
    status: Literal["waiting_human", "executing", "succeeded", "rejected", "expired", "invalidated", "failed", "uncertain"]
    created_at: int
    expires_at: int
    request_id: str
    error_code: str | None
    summary: ApprovalSummary
    result: dict | None


class ApprovalList(BaseModel):
    approvals: list[ApprovalInfo]


def credential(request):
    if getattr(request.state, "admin_auth", None) == "session":
        user = getattr(request.state, "user", None) or {}
        if user.get("role") != "knowledge_admin" or not user.get("id"):
            raise AuthFailure(401, "ADMIN_AUTH_REQUIRED", "管理员权限已改变，请重新登录。")
        # 执行前再读库，避免请求中途被降权后仍用旧会话确认。
        if request.app.state.auth_store.current_role(user["id"]) != "knowledge_admin":
            raise AuthFailure(401, "ADMIN_AUTH_REQUIRED", "管理员权限已改变，请重新登录。")
        return digest("role-session:" + user["id"])
    supplied = request.headers.get("authorization", "")
    current = os.getenv("ADMIN_PASSWORD", "")
    if not current or not hmac.compare_digest(supplied.encode(), ("Bearer " + current).encode()):
        raise AuthFailure(401, "ADMIN_AUTH_REQUIRED", "管理员权限已改变，请重新登录。")
    return digest(current)


def require_approvals(request):
    if not request.app.state.auth_required:
        raise AuthFailure(409, "APPROVALS_REQUIRE_AUTH_MODE", "确认流程仅在正式登录模式启用。")
    return request.app.state.approval_store, credential(request)


class ApprovalStore:
    def __init__(self, database):
        self.db = database

    def setup(self):
        with schema_lock_sync(self.db.url), self.db.connection() as execute:
            execute("CREATE TABLE IF NOT EXISTS hp_approvals (id TEXT PRIMARY KEY, credential_hash TEXT NOT NULL, action TEXT NOT NULL, document_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL, created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL, request_id TEXT NOT NULL, execution_token TEXT, result TEXT, error_code TEXT)")
            execute("CREATE INDEX IF NOT EXISTS hp_approvals_created ON hp_approvals(created_at)")

    @staticmethod
    def public(row):
        return {**{key: row[key] for key in ("id", "action", "document_id", "fingerprint", "status", "created_at", "expires_at", "request_id", "error_code")},
                "summary": json.loads(row["summary"]), "result": json.loads(row["result"]) if row["result"] else None}

    def expire(self, execute):
        now_ms = int(time.time() * 1000)
        for previous, status, code in (("waiting_human", "expired", "APPROVAL_EXPIRED"), ("executing", "uncertain", "APPROVAL_EXECUTION_UNKNOWN")):
            rows = execute("SELECT id FROM hp_approvals WHERE status = ? AND expires_at <= ?", (previous, now_ms)).fetchall()
            for row in rows:
                changed = execute("UPDATE hp_approvals SET status = ?, error_code = ? WHERE id = ? AND status = ? AND expires_at <= ?",
                    (status, code, row["id"], previous, now_ms)).rowcount
                if changed:
                    self.db._audit(execute, "system", "approval." + status, row["id"], uuid.uuid4().hex, 409)

    def create(self, epoch, action, document_id, fingerprint, summary, request_id):
        ident, now_ms = uuid.uuid4().hex, int(time.time() * 1000)
        with self.db.connection() as execute:
            execute("INSERT INTO hp_approvals (id, credential_hash, action, document_id, fingerprint, summary, status, created_at, expires_at, request_id) VALUES (?, ?, ?, ?, ?, ?, 'waiting_human', ?, ?, ?)",
                (ident, epoch, action, document_id, fingerprint, json.dumps(summary, ensure_ascii=False), now_ms, now_ms + 300_000, request_id))
            self.db._audit(execute, "knowledge-admin", "approval.requested", ident, request_id, 201)
        return self.get(ident, epoch)

    def get(self, ident, epoch):
        with self.db.connection() as execute:
            self.expire(execute)
            row = execute("SELECT * FROM hp_approvals WHERE id = ? AND credential_hash = ?", (ident, epoch)).fetchone()
        if not row:
            raise AuthFailure(404, "APPROVAL_NOT_FOUND", "确认记录不存在或已不属于当前管理员凭证。")
        return self.public(row)

    def list(self, epoch):
        with self.db.connection() as execute:
            self.expire(execute)
            return [self.public(row) for row in execute("SELECT * FROM hp_approvals WHERE credential_hash = ? ORDER BY created_at DESC LIMIT 50", (epoch,)).fetchall()]

    def decide(self, ident, epoch, decision, fingerprint, request_id):
        # 过期状态独立提交，拒绝执行时不会把过期标记回滚。
        self.get(ident, epoch)
        with self.db.connection() as execute:
            suffix = " FOR UPDATE" if self.db.url else ""
            row = execute("SELECT * FROM hp_approvals WHERE id = ? AND credential_hash = ?" + suffix, (ident, epoch)).fetchone()
            if not row or row["fingerprint"] != fingerprint:
                raise AuthFailure(409, "APPROVAL_MISMATCH", "确认内容不匹配，请重新核对。")
            if row["status"] in {"succeeded", "rejected"} and ((decision == "approve") == (row["status"] == "succeeded")):
                return self.public(row), None
            if row["status"] != "waiting_human":
                raise AuthFailure(409, row["error_code"] or "APPROVAL_NOT_PENDING", "此确认已处理或执行中，请查看确认记录，不会重复执行。")
            if row["expires_at"] <= int(time.time() * 1000):
                raise AuthFailure(409, "APPROVAL_EXPIRED", "确认已过期，请重新发起。")
            token = uuid.uuid4().hex if decision == "approve" else None
            status = "executing" if token else "rejected"
            execute("UPDATE hp_approvals SET status = ?, execution_token = ? WHERE id = ?", (status, token, ident))
            self.db._audit(execute, "knowledge-admin", "approval." + decision, ident, request_id, 200)
            return {**self.public(row), "status": status}, token

    def fail(self, ident, token, status, code, request_id):
        with self.db.connection() as execute:
            changed = execute("UPDATE hp_approvals SET status = ?, error_code = ? WHERE id = ? AND execution_token = ? AND status = 'executing'",
                (status, code, ident, token)).rowcount
            if changed:
                self.db._audit(execute, "knowledge-admin", "approval." + status, ident, request_id, 409)

    def finish_memory(self, ident, token, result, request_id):
        with self.db.connection() as execute:
            changed = execute("UPDATE hp_approvals SET status = 'succeeded', result = ? WHERE id = ? AND execution_token = ? AND status = 'executing' AND expires_at > ?",
                (json.dumps(result, ensure_ascii=False), ident, token, int(time.time() * 1000))).rowcount
            if not changed:
                raise AuthFailure(409, "APPROVAL_EXPIRED", "确认已过期或被处理，操作未执行。")
            self.db._audit(execute, "knowledge-admin", "approval.succeeded", ident, request_id, 200)


async def execute_approval(request, ident, body):
    store, epoch = require_approvals(request)
    row, token = await asyncio.to_thread(store.decide, ident, epoch, body.decision, body.fingerprint, request.state.request_id)
    if not token:
        return row
    async def finalize(conn, result):
        if credential(request) != epoch:
            raise AuthFailure(403, "APPROVAL_AUTH_CHANGED", "管理员权限已改变，操作未执行。")
        if row["expires_at"] <= int(time.time() * 1000):
            raise AuthFailure(409, "APPROVAL_EXPIRED", "确认已过期，操作未执行。")
        if conn:
            cursor = await conn.execute("UPDATE hp_approvals SET status = 'succeeded', result = %s WHERE id = %s AND execution_token = %s AND status = 'executing' AND expires_at > %s",
                (json.dumps(result, ensure_ascii=False), ident, token, int(time.time() * 1000)))
            if not cursor.rowcount:
                raise AuthFailure(409, "APPROVAL_NOT_PENDING", "确认状态已改变，操作未执行。")
            await conn.execute("INSERT INTO hp_audit VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (uuid.uuid4().hex, int(time.time()), "knowledge-admin", "approval.succeeded", ident, request.state.request_id, 200))
        else:
            await asyncio.to_thread(store.finish_memory, ident, token, result, request.state.request_id)
    try:
        async with asyncio.timeout(120):
            await apply_review(request.app.state.agent.kb, row["document_id"], row["action"], row["fingerprint"], finalize, access=row["summary"].get("access"))
    except asyncio.CancelledError:
        # 提交响应丢失时保留执行状态，由查询核对；不得假定知识变更未生效。
        raise
    except Exception as exc:
        code = exc.code if isinstance(exc, AuthFailure) else "APPROVAL_TIMEOUT" if isinstance(exc, TimeoutError) else "APPROVAL_EXECUTION_FAILED"
        status = "expired" if code == "APPROVAL_EXPIRED" else "invalidated" if code in {"APPROVAL_STALE", "APPROVAL_AUTH_CHANGED", "ADMIN_AUTH_REQUIRED"} else "failed"
        await asyncio.to_thread(store.fail, ident, token, status, code, request.state.request_id)
        if isinstance(exc, AuthFailure):
            raise
        raise AuthFailure(409, code, "操作未完成，请核对确认记录和文档状态后重新发起确认。") from None
    return await asyncio.to_thread(store.get, ident, epoch)


def attach_approval_api(router):
    @router.post("/admin/approvals", status_code=201, response_model=ApprovalInfo, summary="生成待人工核对的知识变更确认单")
    async def create(request: Request, body: ApprovalRequest):
        store, epoch = require_approvals(request)
        async def create_approval():
            access = body.access.model_dump() if body.access else None
            if access:
                def check_accounts():
                    with store.db.connection() as execute:
                        for user_id in access["allowed_user_ids"]:
                            if not execute("SELECT id FROM hp_users WHERE id = ?", (user_id,)).fetchone():
                                raise AuthFailure(422, "UNKNOWN_ACCOUNT", "指定账号不存在。")
                await asyncio.to_thread(check_accounts)
            fingerprint, summary = await preview(request.app.state.agent.kb, body.document_id, body.action, access=access)
            return await asyncio.to_thread(store.create, epoch, body.action, body.document_id, fingerprint, summary, request.state.request_id)
        return await idempotent(request, "approvals:" + epoch[:12], body.model_dump(), create_approval)

    @router.get("/admin/approvals", response_model=ApprovalList, summary="查询当前管理员凭证下的确认记录")
    async def list_approvals(request: Request):
        store, epoch = require_approvals(request)
        return {"approvals": await asyncio.to_thread(store.list, epoch)}

    @router.get("/admin/approvals/{ident}", response_model=ApprovalInfo)
    async def get_approval(request: Request, ident: str):
        store, epoch = require_approvals(request)
        return await asyncio.to_thread(store.get, ident, epoch)

    @router.post("/admin/approvals/{ident}/decision", response_model=ApprovalInfo, summary="拒绝或确认并立即执行知识变更")
    async def decide(request: Request, ident: str, body: ApprovalDecision):
        return await execute_approval(request, ident, body)
