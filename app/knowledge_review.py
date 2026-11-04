"""在知识库写锁内校验待确认内容，并原子应用发布或删除。"""
import copy
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager

from app.knowledge import KnowledgeError, now
from app.auth import AuthFailure


@asynccontextmanager
async def locked(kb):
    if kb.backend == "pgvector":
        async with kb.connection() as conn:
            await kb.lock_writes(conn)
            yield conn
    else:
        async with kb.lock:
            yield None


async def snapshot(kb, conn, doc_id, action):
    document = await kb.document(conn, doc_id) if conn else kb.public_document(kb.require(doc_id), True)
    if action == "publish" and document["status"] != "draft":
        raise KnowledgeError("只允许修改草稿。")
    if action == "delete" and document["builtin"]:
        raise KnowledgeError("内置文档不能删除。")
    replaced = []
    if action == "publish":
        if conn:
            rows = await (await conn.execute("SELECT id FROM knowledge_documents WHERE title = %s AND variant = %s AND status = 'published' AND id <> %s ORDER BY id",
                (document["title"], document.get("variant", "standard"), doc_id))).fetchall()
            replaced = [await kb.document(conn, row["id"]) for row in rows]
        else:
            replaced = [kb.public_document(row, True) for key, row in sorted(kb.documents.items())
                if key != doc_id and row["title"] == document["title"] and row["status"] == "published"
                and row.get("variant", "standard") == document.get("variant", "standard")]
    for item in [document, *replaced]:
        item["chunks"] = sorted(item["chunks"], key=lambda chunk: (chunk["seq"], chunk["id"]))
    digest = hashlib.sha256(json.dumps({"action": action, "document": document, "replaced": replaced},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    summary = {"document_id": doc_id, "title": document["title"], "version": document["version"],
        "chunk_count": document["chunk_count"], "action": action,
        "replaced": [{key: row[key] for key in ("id", "title", "version")} for row in replaced],
        "risk": "发布后内容将进入医学问答检索；同标题同版本类型的旧文档将删除。" if action == "publish" else "文档和全部片段将永久删除，后续回答无法再检索这些内容。"}
    return document, replaced, digest, summary


async def preview(kb, doc_id, action):
    async with locked(kb) as conn:
        _, _, digest, summary = await snapshot(kb, conn, doc_id, action)
        return digest, summary


async def apply_review(kb, doc_id, action, expected_digest, finalize):
    async with locked(kb) as conn:
        document, replaced, digest, _ = await snapshot(kb, conn, doc_id, action)
        if digest != expected_digest:
            raise AuthFailure(409, "APPROVAL_STALE", "文档或受影响的旧版本已改变，请重新核对并发起确认。")
        vectors = await kb.publication_vectors(document) if action == "publish" else []
        if conn:
            if action == "publish":
                for chunk, vector in zip(document["chunks"], vectors):
                    await conn.execute("UPDATE knowledge_chunks SET embedding = %s::vector WHERE id = %s",
                        (json.dumps(vector) if vector is not None else None, chunk["id"]))
                await conn.execute("UPDATE knowledge_documents SET status = 'published', published_at = now() WHERE id = %s", (doc_id,))
                for old in replaced:
                    await conn.execute("DELETE FROM knowledge_documents WHERE id = %s", (old["id"],))
                result = await kb.document(conn, doc_id)
            else:
                await conn.execute("DELETE FROM knowledge_documents WHERE id = %s", (doc_id,))
                result = {"ok": True}
            # 知识变更、审批终态和执行审计共用当前数据库事务。
            await finalize(conn, result)
        else:
            documents = copy.deepcopy(kb.documents)
            if action == "publish":
                row = documents[doc_id]
                for chunk, vector in zip(row["chunks"], vectors):
                    chunk["embedding"] = vector
                row.update(status="published", published_at=now())
                for old in replaced:
                    del documents[old["id"]]
                result = kb.public_document(row, True)
            else:
                del documents[doc_id]
                result = {"ok": True}
            finalization = asyncio.create_task(finalize(None, result))
            try:
                await asyncio.shield(finalization)
            except asyncio.CancelledError:
                # SQLite 提交不能被线程取消；确认其结果后同步内存，避免成功记录与内存脱节。
                await finalization
                kb.documents = documents
                raise
            # 本地内存模式只提供进程内一致性，不宣称数据库持久化。
            kb.documents = documents
        return result
