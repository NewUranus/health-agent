"""任务提交与事件订阅；订阅断开不取消后台任务。"""
import asyncio
import json
import re
from typing import Literal

from fastapi import Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthFailure, COOKIE_NAME
from app.http_security import current_identity
from app.tasks import TERMINAL


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=12000)
    web_search: bool = False


class TaskInfo(BaseModel):
    id: str
    thread_id: str
    status: Literal["pending", "running", "waiting_human", "succeeded", "failed", "cancelled", "timed_out", "interrupted"]
    request_id: str
    error_code: str | None
    created_at: int
    updated_at: int
    cancel_requested: bool
    health_committed: bool | None = None
    failure_category: str | None = None
    review_disposition: Literal["resolved", "needs_support"] | None = None


class TaskReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disposition: Literal["resolved", "needs_support"]


class TaskList(BaseModel):
    tasks: list[TaskInfo]


class EventPage(BaseModel):
    task: TaskInfo
    events: list[dict]


def require_tasks(request):
    if not request.app.state.auth_required:
        raise AuthFailure(409, "TASKS_REQUIRE_LOGIN", "持久化任务需要启用账号登录模式。")
    current_identity(request)
    return request.app.state.task_store, request.state.user["id"]


def attach_task_api(router):
    @router.post("/tasks", response_model=TaskInfo, status_code=202, summary="幂等提交一个对话任务")
    async def submit(request: Request, body: TaskRequest, response: Response,
                     idempotency_key: str = Header(min_length=8, max_length=128)):
        store, user_id = require_tasks(request)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", idempotency_key):
            raise AuthFailure(422, "INVALID_IDEMPOTENCY_KEY", "幂等键须为 8–128 位字母、数字或 . _ : -。")
        await asyncio.to_thread(store.db.claim_thread, body.thread_id, user_id)
        task, created = await asyncio.to_thread(store.submit, user_id, idempotency_key, body.model_dump(), request.state.request_id)
        response.status_code = 202 if created else 200
        response.headers["Location"] = "/api/v1/tasks/" + task["id"]
        return task

    @router.get("/tasks", response_model=TaskList, summary="查看自己的最近任务")
    async def list_tasks(request: Request, limit: int = Query(30, ge=1, le=100), failed: bool = False):
        store, user_id = require_tasks(request)
        return {"tasks": await asyncio.to_thread(store.list, user_id, limit, failed)}

    @router.post("/tasks/{task_id}/review", response_model=TaskInfo, summary="记录本人失败任务的核对结果，不重新执行")
    async def review_task(request: Request, task_id: str, body: TaskReview):
        store, user_id = require_tasks(request)
        return await asyncio.to_thread(store.review, task_id, user_id, body.disposition, request.state.request_id)

    @router.get("/tasks/{task_id}", response_model=TaskInfo, summary="查看任务状态")
    async def get_task(request: Request, task_id: str):
        store, user_id = require_tasks(request)
        return await asyncio.to_thread(store.get, task_id, user_id)

    @router.post("/tasks/{task_id}/cancel", response_model=TaskInfo, summary="取消排队任务或请求运行任务协作停止")
    async def cancel_task(request: Request, task_id: str):
        store, user_id = require_tasks(request)
        return await asyncio.to_thread(store.cancel, task_id, user_id, request.state.request_id)

    @router.get("/tasks/{task_id}/event-page", response_model=EventPage, summary="按序号读取已保存的事件")
    async def event_page(request: Request, task_id: str, after: int = Query(0, ge=0)):
        store, user_id = require_tasks(request)
        task, events = await asyncio.to_thread(store.events, task_id, user_id, after)
        return {"task": task, "events": events}

    @router.get("/tasks/{task_id}/events", summary="从指定事件序号继续读取任务结果")
    async def task_events(request: Request, task_id: str, after: int = Query(0, ge=0)):
        store, user_id = require_tasks(request)
        await asyncio.to_thread(store.get, task_id, user_id)
        async def stream():
            cursor = after
            while True:
                if await request.is_disconnected():
                    return
                if not await asyncio.to_thread(store.db.resolve, request.cookies.get(COOKIE_NAME)):
                    yield "data: " + json.dumps({"type": "error", "message": "登录已失效，请重新登录。", "code": "AUTH_REQUIRED"}, ensure_ascii=False) + "\n\n"
                    yield 'data: {"type":"done"}\n\n'
                    return
                task, rows = await asyncio.to_thread(store.events, task_id, user_id, cursor)
                for row in rows:
                    cursor = row["seq"]
                    yield f"id: {cursor}\ndata: " + json.dumps({**row["event"], "seq": cursor}, ensure_ascii=False) + "\n\n"
                if task["status"] in TERMINAL and len(rows) < 200:
                    return
                if not rows:
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(0.25)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})
