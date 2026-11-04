"""只暴露运行元数据的运维接口；管理操作由后台鉴权和审计保护。"""
import asyncio
import json
import os
import time

from fastapi import Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from app.auth import AuthFailure
from app.telemetry import status as log_status


def require_database(request):
    if not request.app.state.auth_required:
        raise AuthFailure(409, "OPERATIONS_REQUIRE_LOGIN", "运维面板需要启用正式登录模式。")
    return request.app.state.auth_store


def snapshot(database):
    current = int(time.time())
    with database.connection() as execute:
        rows = execute("SELECT status, COUNT(*) AS count, MIN(created_at) AS oldest FROM hp_tasks GROUP BY status").fetchall()
        workers = execute("SELECT id, slots, last_seen FROM hp_workers WHERE last_seen >= ? ORDER BY last_seen DESC", (current - 20,)).fetchall()
        failures = execute("SELECT error_code, COUNT(*) AS count FROM hp_tasks WHERE created_at >= ? AND status IN ('failed', 'timed_out', 'interrupted') GROUP BY error_code", ((current - 86400) * 1000000000,)).fetchall()
        recent = execute("SELECT COUNT(*) AS completed, AVG(updated_at - created_at) AS average_ns FROM hp_tasks WHERE status IN ('succeeded', 'waiting_human') AND updated_at >= ?", ((current - 86400) * 1000000000,)).fetchone()
        stale = execute("SELECT COUNT(*) AS count FROM hp_tasks WHERE status = 'running' AND lease_until < ?", (current,)).fetchone()["count"]
        paused = execute("SELECT value FROM hp_system_settings WHERE name = 'admission_paused'").fetchone()
        reviews = execute("SELECT COUNT(*) AS count FROM hp_task_reviews WHERE disposition = 'needs_support'").fetchone()["count"]
        costs = execute("SELECT SUM(c.cost_microusd) AS cost_microusd, COUNT(*) AS priced_calls FROM hp_model_costs c JOIN hp_model_usage u ON u.id = c.usage_id WHERE u.at >= ?", (current - 86400,)).fetchone()
    counts = {row["status"]: row["count"] for row in rows}
    oldest = next((max(0, current - row["oldest"] // 1000000000) for row in rows if row["status"] == "pending"), 0)
    alerts = []
    if not workers:
        alerts.append({"code": "NO_WORKER", "level": "critical", "message": "未发现有效 Worker 心跳。检查 Worker 服务及数据库连接。"})
    if stale:
        alerts.append({"code": "STALE_LEASE", "level": "warning", "message": "存在租约过期任务，检查 Worker 恢复记录。"})
    if oldest > int(os.getenv("HEALTH_AGENT_QUEUE_ALERT_SECONDS", "60")):
        alerts.append({"code": "QUEUE_DELAY", "level": "warning", "message": "排队时间超过阈值。检查模型延迟、Worker 容量及限额。"})
    return {"at": current, "release": os.getenv("HEALTH_AGENT_RELEASE", "local"), "tasks": counts,
        "workers": [dict(row) for row in workers], "oldest_pending_seconds": oldest, "stale_tasks": stale,
        "completed_24h": recent["completed"], "average_completion_seconds_24h": float(recent["average_ns"] or 0) / 1000000000,
        "failures_24h": [dict(row) for row in failures], "needs_support": reviews,
        "admission_paused": bool(paused and paused["value"] == "1"), "costs_24h": dict(costs), "alerts": alerts}


class AdmissionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool


class PriorityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: int = Field(ge=0, le=9)


def attach_operations(router):
    @router.get("/ready", summary="就绪检查，不调用健康计算或模型")
    async def ready(request: Request):
        try:
            async with asyncio.timeout(5):
                agent = request.app.state.agent
                if agent.graph is None:
                    raise RuntimeError()
                await agent.mcp.list_tools()
                if request.app.state.auth_required:
                    def database_ready():
                        with request.app.state.auth_store.connection() as execute:
                            return execute("SELECT COUNT(*) AS count FROM hp_workers WHERE last_seen >= ?", (int(time.time()) - 20,)).fetchone()["count"] > 0
                    if not await asyncio.to_thread(database_ready):
                        raise RuntimeError()
            return {"ready": True}
        except Exception:
            return JSONResponse({"ready": False}, status_code=503)

    @router.get("/admin/operations", summary="查看任务队列、Worker、失败分类与本地告警")
    async def operations(request: Request):
        return await asyncio.to_thread(snapshot, require_database(request))

    @router.get("/admin/queue", summary="查看最近排队任务的调度信息，不返回健康正文")
    async def queue(request: Request):
        database = require_database(request)
        def read():
            with database.connection() as execute:
                return [dict(row) for row in execute("SELECT t.id, t.status, t.request_id, t.created_at, COALESCE(p.priority, 0) AS priority FROM hp_tasks t LEFT JOIN hp_task_priorities p ON p.task_id = t.id WHERE t.status IN ('pending', 'running') ORDER BY COALESCE(p.priority, 0) DESC, t.created_at LIMIT 100").fetchall()]
        return {"tasks": await asyncio.to_thread(read)}

    @router.patch("/admin/admission", summary="暂停或恢复新任务接收，已接收任务继续执行")
    async def admission(request: Request, body: AdmissionUpdate):
        database = require_database(request)
        def save():
            with database.connection() as execute:
                execute("SELECT id FROM hp_queue_gate WHERE id = 1" + (" FOR UPDATE" if database.url else "")).fetchone()
                execute("UPDATE hp_system_settings SET value = ? WHERE name = 'admission_paused'", ("1" if body.paused else "0",))
                database._audit(execute, "knowledge-admin", "queue.paused" if body.paused else "queue.resumed", "admission", request.state.request_id, 200)
        await asyncio.to_thread(save)
        return {"paused": body.paused}

    @router.patch("/admin/queue/{task_id}/priority", summary="调整尚未执行任务的调度优先级")
    async def priority(request: Request, task_id: str, body: PriorityUpdate):
        database = require_database(request)
        def save():
            with database.connection() as execute:
                row = execute("SELECT status FROM hp_tasks WHERE id = ?" + (" FOR UPDATE" if database.url else ""), (task_id,)).fetchone()
                if not row:
                    raise AuthFailure(404, "TASK_NOT_FOUND", "任务不存在。")
                if row["status"] != "pending":
                    raise AuthFailure(409, "TASK_NOT_PENDING", "只能调整尚未执行的任务。")
                execute("INSERT INTO hp_task_priorities VALUES (?, ?) ON CONFLICT (task_id) DO UPDATE SET priority = excluded.priority", (task_id, body.priority))
                database._audit(execute, "knowledge-admin", "queue.priority", task_id, request.state.request_id, 200)
        await asyncio.to_thread(save)
        return {"id": task_id, "priority": body.priority}

    @router.get("/admin/audit", summary="按请求编号查询元数据审计")
    async def audit(request: Request, request_id: str = Query("", max_length=128), offset: int = Query(0, ge=0)):
        database = require_database(request)
        def read():
            with database.connection() as execute:
                return [dict(row) for row in execute("SELECT id, at, actor, action, resource, request_id, status FROM hp_audit WHERE (? = '' OR request_id = ?) ORDER BY at DESC, id DESC LIMIT 100 OFFSET ?", (request_id, request_id, offset)).fetchall()]
        return {"records": await asyncio.to_thread(read)}

    @router.get("/admin/metrics", summary="管理员鉴权的 Prometheus 指标")
    async def metrics(request: Request):
        data = await asyncio.to_thread(snapshot, require_database(request))
        lines = ["# TYPE healthpilot_tasks gauge"]
        for status, count in data["tasks"].items():
            lines.append(f'healthpilot_tasks{{status={json.dumps(status)}}} {count}')
        for name, value in {"workers": len(data["workers"]), "queue_oldest_seconds": data["oldest_pending_seconds"], "stale_tasks": data["stale_tasks"], "admission_paused": int(data["admission_paused"])}.items():
            lines.extend([f"# TYPE healthpilot_{name} gauge", f"healthpilot_{name} {value}"])
        logs = await asyncio.to_thread(log_status)
        for name, value in {"log_buffer_pending": logs.get("pending"), "log_dropped_total": logs.get("dropped"),
                "log_export_unavailable": int(bool(logs.get("export_error")))}.items():
            if value is not None:
                lines.extend([f"# TYPE healthpilot_{name} gauge", f"healthpilot_{name} {value}"])
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
