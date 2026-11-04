"""账号私有的会话工作台、历史轮次和经权限复验的导出。"""
import asyncio
import json
import time

from fastapi import Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from app.auth import AuthFailure


class ConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=80)
    archived: bool | None = None


class ConversationStore:
    def __init__(self, database):
        self.db = database

    def require(self, execute, ident, user_id):
        row = execute("SELECT * FROM hp_conversations WHERE id = ? AND user_id = ?", (ident, user_id)).fetchone()
        if not row:
            raise AuthFailure(404, "CONVERSATION_NOT_FOUND", "会话不存在或无权访问。")
        return dict(row)

    def list(self, user_id, archived, query, offset):
        with self.db.connection() as execute:
            rows = execute("""SELECT c.*, (SELECT COUNT(*) FROM hp_tasks t WHERE t.user_id = c.user_id AND t.thread_id = c.thread_id) AS turns,
                (SELECT status FROM hp_tasks t WHERE t.user_id = c.user_id AND t.thread_id = c.thread_id ORDER BY created_at DESC, id DESC LIMIT 1) AS last_status
                FROM hp_conversations c WHERE c.user_id = ? AND c.archived = ? AND LOWER(c.title) LIKE LOWER(?)
                ORDER BY c.updated_at DESC, c.id LIMIT 30 OFFSET ?""", (user_id, int(archived), "%" + query + "%", offset)).fetchall()
        return [{**dict(row), "updated_at": row["updated_at"] // 1000000, "archived": bool(row["archived"])} for row in rows]

    def update(self, ident, user_id, body, request_id):
        with self.db.connection() as execute:
            row = self.require(execute, ident, user_id)
            title = body.title.strip() if body.title is not None else row["title"]
            if not title:
                raise AuthFailure(422, "INVALID_TITLE", "会话标题不能为空。")
            archived = int(body.archived) if body.archived is not None else row["archived"]
            execute("UPDATE hp_conversations SET title = ?, archived = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (title, archived, time.time_ns(), ident, user_id))
            self.db._audit(execute, user_id, "conversation.updated", ident, request_id, 200)
        return {"id": ident, "title": title, "archived": bool(archived)}

    def turns(self, ident, user_id, before, limit):
        with self.db.connection() as execute:
            conversation = self.require(execute, ident, user_id)
            # 使用时间和 ID 的复合游标，时间相同的轮次也不会漏读。
            cursor_time, cursor_id = (before.split(":", 1) if before else (str(2**63 - 1), "~"))
            try:
                cursor_time = int(cursor_time)
            except ValueError:
                raise AuthFailure(422, "INVALID_CURSOR", "会话分页游标无效。") from None
            if not 0 <= cursor_time < 2**63 or len(cursor_id) > 64:
                raise AuthFailure(422, "INVALID_CURSOR", "会话分页游标无效。")
            rows = execute("""SELECT id, thread_id, payload, status, created_at FROM hp_tasks WHERE user_id = ? AND thread_id = ?
                AND (created_at < ? OR (created_at = ? AND id < ?)) ORDER BY created_at DESC, id DESC LIMIT ?""",
                (user_id, conversation["thread_id"], cursor_time, cursor_time, cursor_id, limit)).fetchall()
            turns = []
            for row in reversed(rows):
                events = execute("SELECT payload FROM hp_task_events WHERE task_id = ? ORDER BY seq LIMIT 201", (row["id"],)).fetchall()
                turns.append({"id": row["id"], "message": json.loads(row["payload"])["message"], "status": row["status"],
                    "created_at": row["created_at"] // 1000000, "events": [json.loads(item["payload"]) for item in events[:200]], "truncated": len(events) > 200})
        next_cursor = f'{rows[-1]["created_at"]}:{rows[-1]["id"]}' if len(rows) == limit else None
        return {"conversation": {key: conversation[key] for key in ("id", "title", "thread_id", "archived")}, "turns": turns, "next_cursor": next_cursor}


def attach_conversations(router):
    from app.task_api import require_tasks

    async def read_turns(request, ident, before, limit):
        tasks, user_id = require_tasks(request)
        result = await asyncio.to_thread(ConversationStore(tasks.db).turns, ident, user_id, before, limit)
        for turn in result["turns"]:
            turn["events"] = await request.app.state.agent.kb.sanitize_events(turn["events"], "account:" + user_id)
        return result

    @router.get("/conversations", summary="查询本人会话")
    async def conversations(request: Request, archived: bool = False, q: str = Query("", max_length=80), offset: int = Query(0, ge=0)):
        tasks, user_id = require_tasks(request)
        return {"conversations": await asyncio.to_thread(ConversationStore(tasks.db).list, user_id, archived, q, offset)}

    @router.patch("/conversations/{ident}", summary="重命名、归档或恢复本人会话")
    async def update(request: Request, ident: str, body: ConversationUpdate):
        tasks, user_id = require_tasks(request)
        return await asyncio.to_thread(ConversationStore(tasks.db).update, ident, user_id, body, request.state.request_id)

    @router.get("/conversations/{ident}/turns", summary="分页恢复本人会话，重新校验知识访问权限")
    async def turns(request: Request, ident: str, before: str | None = Query(None, max_length=100, pattern=r"^\d{1,19}:[a-f0-9]{32}$"), limit: int = Query(20, ge=1, le=50)):
        return await read_turns(request, ident, before, limit)

    @router.get("/conversations/{ident}/export", summary="导出本人最近五十轮对话及当前可见来源")
    async def export(request: Request, ident: str):
        result = await read_turns(request, ident, None, 50)
        lines = ["# " + result["conversation"]["title"], "", "仅为技术演示，不构成医疗建议。此文件包含个人对话，请妥善保管。", ""]
        if result["next_cursor"]:
            lines.append("本次仅导出最近 50 轮，更多内容请在工作台分页查看。")
        for turn in result["turns"]:
            lines.extend(["## 用户", turn["message"], "## HealthPilot"])
            sources = {}
            for event in turn["events"]:
                if event["type"] == "message":
                    lines.append(event["content"])
                if event["type"] == "error":
                    lines.append(event["message"])
                for chunk in event.get("data", {}).get("chunks", []):
                    sources[chunk["id"]] = chunk
            if sources:
                lines.append("### 来源")
                for item in sources.values():
                    lines.append(f'- {item["id"]}：{item["title"]}；版本 {item.get("version") or "未标注"}；页码 {item.get("page") or "未标注"}；{item["source"]}')
            if turn["truncated"]:
                lines.append("此轮过程较长，仅导出前 200 个事件，请到任务记录查看完整过程。")
        content = "\n\n".join(lines)
        if len(content) > 2000000:
            raise AuthFailure(413, "EXPORT_TOO_LARGE", "导出内容过大，请在任务记录分轮查看。")
        return Response(content, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="healthpilot-{ident}.md"', "Cache-Control": "no-store"})
