"""保留已确认知识版本；档案只供管理员核对，不参与用户检索。"""
import copy
import hashlib
import json
from datetime import datetime, timezone

from fastapi import Query, Request
from pydantic import BaseModel, ConfigDict, Field
from app.idempotency import idempotent
from app.knowledge import KnowledgeError
from app.ingest import chunk_pages


def entry(document, reason):
    payload = copy.deepcopy(document)
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"id": fingerprint, "document_id": document["id"], "title": document["title"],
            "version": document["version"], "reason": reason, "archived_at": datetime.now(timezone.utc).isoformat(), "document": payload}


async def save(conn, snapshot):
    await conn.execute("INSERT INTO knowledge_archives (id, document_id, payload) VALUES (%s, %s, %s::jsonb) ON CONFLICT (id) DO NOTHING",
                       (snapshot["id"], snapshot["document_id"], json.dumps(snapshot, ensure_ascii=False)))


async def read(kb, archive_id=None, offset=0):
    if kb.backend == "pgvector":
        async with kb.connection() as conn:
            if archive_id:
                rows = await (await conn.execute("SELECT payload FROM knowledge_archives WHERE id = %s", (archive_id,))).fetchall()
            else:
                rows = await (await conn.execute("SELECT payload FROM knowledge_archives ORDER BY created_at DESC, id LIMIT 50 OFFSET %s", (offset,))).fetchall()
        items = [row["payload"] for row in rows]
    else:
        items = [copy.deepcopy(value) for value in reversed(list(kb.archives.values())) if not archive_id or value["id"] == archive_id]
        if not archive_id:
            items = items[offset:offset + 50]
    if archive_id:
        if not items:
            raise KnowledgeError("文档不存在。")
        return items[0]
    return [{key: value for key, value in item.items() if key != "document"} for item in items]


class TextDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    source: str = Field(default="", max_length=2000)
    version: str = Field(default="", max_length=100)
    text: str = Field(min_length=1, max_length=200000)


def attach_knowledge_archive(router):
    @router.get("/admin/knowledge-archives", summary="分页查看知识版本档案")
    async def archives(request: Request, offset: int = Query(0, ge=0)):
        return {"archives": await read(request.app.state.agent.kb, offset=offset)}

    @router.get("/admin/knowledge-archives/{archive_id}", summary="查看历史版本原文，仅管理员")
    async def archive(request: Request, archive_id: str):
        return await read(request.app.state.agent.kb, archive_id)

    @router.post("/admin/text-documents", status_code=201, summary="将纯文本或 Markdown 导入待审核草稿")
    async def text_document(request: Request, body: TextDocument):
        chunks = chunk_pages([{"page": 1, "text": body.text}])
        if not body.title.strip() or not chunks:
            raise KnowledgeError("标题与正文不能为空。")
        async def create():
            return await request.app.state.agent.kb.create_draft(body.title.strip(), body.source, body.version, chunks, "text-import.md")
        return await idempotent(request, "text-documents", body.model_dump(), create)
