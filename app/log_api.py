"""管理员集中日志查询；只读、限时窗、游标分页，不返回业务正文。"""
import asyncio
import time
from typing import Literal
from fastapi import Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from app.auth import AuthFailure
from app.operations import require_database
from app.telemetry import status
from app.telemetry_store import LogStore


class LogQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    limit: int = Field(default=100, ge=1, le=200)
    before: str | None = Field(default=None, pattern=r"^[0-9]{1,16}:[0-9a-f]{32}$")
    q: str = Field(default="", max_length=100)
    trace_id: str = Field(default="", pattern=r"^([0-9a-f]{32})?$")
    request_id: str = Field(default="", pattern=r"^([0-9a-f]{32})?$")
    task_id: str = Field(default="", pattern=r"^([0-9a-f]{32})?$")
    service: Literal["", "api", "worker", "mcp"] = ""
    level: Literal["", "INFO", "WARNING", "ERROR", "CRITICAL"] = ""
    event: str = Field(default="", max_length=100, pattern=r"^[a-zA-Z0-9_.-]*$")


async def read_logs(**query):
    try:
        return await asyncio.to_thread(LogStore().search, **query)
    except Exception:
        raise AuthFailure(503, "LOG_STORE_UNAVAILABLE", "集中日志暂不可用，请查看运行状态并稍后重试。") from None


def attach_log_api(router):
    @router.get("/admin/logs", summary="集中日志检索，最多查询三十天并按游标分页")
    async def logs(request: Request, query: LogQuery = Query()):
        require_database(request)
        values = query.model_dump()
        end = query.end if query.end is not None else int(time.time() * 1000)
        start = query.start if query.start is not None else end - 86400000
        if start > end or end - start > 30 * 86400000:
            raise AuthFailure(422, "LOG_TIME_RANGE", "查询时间范围须在三十天以内，起点不能晚于终点。")
        values.update(start=start, end=end)
        return {**await read_logs(**values), "start": start, "end": end}

    @router.get("/admin/logs/status", summary="本实例日志缓冲与中心各服务最近入库时间")
    async def log_status(request: Request):
        require_database(request)
        local = await asyncio.to_thread(status)
        def services():
            with LogStore().connection() as execute:
                return [dict(row) for row in execute("SELECT service, MAX(at) AS last_seen FROM hp_logs WHERE at >= ? GROUP BY service", (int(time.time() * 1000) - 86400000,)).fetchall()]
        try:
            seen = await asyncio.to_thread(services)
            return {**local, "central_available": True, "services": seen}
        except Exception:
            return {**local, "central_available": False, "services": []}

    @router.get("/admin/traces/{trace_id}", summary="按 trace_id 获取跨进程链路，支持游标续页")
    async def trace(request: Request, trace_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
                    before: str | None = Query(None, pattern=r"^[0-9]{1,16}:[0-9a-f]{32}$")):
        require_database(request)
        return await read_logs(start=0, end=int(time.time() * 1000), trace_id=trace_id, limit=1000, before=before)
