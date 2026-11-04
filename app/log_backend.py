"""通过 Loki 查询已脱敏的 OTLP 日志；高基数标识不作为索引标签。"""
import json
import os
import re
from urllib.parse import urlsplit
import httpx
from app.auth import AuthFailure


def grafana_url():
    value = os.getenv("HEALTH_AGENT_GRAFANA_URL", "").rstrip("/")
    parsed = urlsplit(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment else None


class LokiStore:
    def __init__(self):
        self.url = os.getenv("HEALTH_AGENT_LOKI_URL", "").rstrip("/")
        if not self.url:
            raise AuthFailure(503, "LOG_BACKEND_NOT_CONFIGURED", "尚未配置 Loki。请启动可观测性服务并设置 HEALTH_AGENT_LOKI_URL。")
        self.headers = {}
        if os.getenv("HEALTH_AGENT_LOKI_TOKEN"):
            self.headers["Authorization"] = "Bearer " + os.environ["HEALTH_AGENT_LOKI_TOKEN"]
        if os.getenv("HEALTH_AGENT_LOKI_TENANT"):
            self.headers["X-Scope-OrgID"] = os.environ["HEALTH_AGENT_LOKI_TENANT"]

    async def search(self, *, start, end, limit=100, before=None, q="", **filters):
        service = filters.get("service")
        selector = '{service_name=' + json.dumps("healthpilot-" + service) + '}' if service else '{service_name=~"healthpilot-(api|worker|mcp)"}'
        query = selector + (' |= ' + json.dumps(q, ensure_ascii=False) if q else '')
        mapping = {"trace_id": "hp_trace", "request_id": "hp_request", "task_id": "hp_task", "thread_id": "hp_thread", "user_hash": "hp_user", "level": "hp_level", "event": "hp_event"}
        query += ' | json ' + ', '.join(alias + '=' + json.dumps(key) for key, alias in mapping.items()) + ' | __error__=""'
        for key, alias in mapping.items():
            if filters.get(key):
                query += ' | ' + alias + '=' + json.dumps(filters[key])
        async with httpx.AsyncClient(timeout=10, headers=self.headers, follow_redirects=False) as client:
            async def rows(low, high, count):
                if high < low:
                    return []
                response = await client.get(self.url + "/loki/api/v1/query_range", params={"query": query, "start": str(low), "end": str(high + 1), "direction": "backward", "limit": count})
                response.raise_for_status()
                data = response.json()
                if data.get("status") != "success":
                    raise RuntimeError("日志查询失败")
                result = []
                for stream in data["data"]["result"]:
                    for timestamp, body, *_ in stream.get("values", []):
                        try:
                            item = json.loads(body)
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(item, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(item.get("id", ""))):
                            continue
                        result.append((int(timestamp), item["id"], item))
                return result

            async def same_time(timestamp):
                group = await rows(timestamp, timestamp, 5000)
                if len(group) >= 5000:
                    raise AuthFailure(422, "LOG_CURSOR_DENSITY", "同一时间点日志过多，请按服务或任务缩小查询范围。")
                return group

            low, high = start * 1000000, end * 1000000 + 999999
            prefix = []
            if before:
                timestamp, ident = before.split(":", 1)
                timestamp = int(timestamp)
                if low <= timestamp <= high:
                    prefix = [row for row in await same_time(timestamp) if row[1] < ident]
                high = min(high, timestamp - 1)
            records = await rows(low, high, limit + 1)
            # 补全分页边界上的同时间戳记录，避免多个服务同刻写入时漏页。
            if len(records) >= limit + 1:
                boundary = min(row[0] for row in records)
                records = [row for row in records if row[0] != boundary] + await same_time(boundary)
            unique = {(row[0], row[1]): row for row in prefix + records}
            ordered = sorted(unique.values(), key=lambda row: (row[0], row[1]), reverse=True)
            page = ordered[:limit]
            return {"records": [row[2] for row in page], "next_cursor": f"{page[-1][0]}:{page[-1][1]}" if len(ordered) > limit else None}

    async def ready(self):
        async with httpx.AsyncClient(timeout=3, headers=self.headers, follow_redirects=False) as client:
            result = await client.get(self.url + "/ready")
            return result.status_code == 200
