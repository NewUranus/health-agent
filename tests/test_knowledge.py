"""知识库接口测试。设置 TEST_DATABASE_URL 时同样的用例会跑在真实 pgvector 上（CI 里有）。"""

import asyncio
import os
import uuid

import pytest

from app.knowledge import KnowledgeError, MemoryKnowledgeBase, PgKnowledgeBase
from app.rag import Chunk

PG_URL = os.getenv("TEST_DATABASE_URL")
BACKENDS = ["memory"] + (["pg"] if PG_URL else [])
VOCAB = ["盐", "运动", "戒烟", "血压", "西瓜"]


def fake_embedder(texts):
    """每个词一维，再加一维常量，保证向量非零；维度固定为 6。"""
    return [[float(word in t) for word in VOCAB] + [0.01] for t in texts]


def broken_embedder(texts):
    raise RuntimeError("quota")


CHUNKS = [
    {"seq": 0, "page": 1, "heading": "第一章", "text": "每天吃盐不超过5克"},
    {"seq": 1, "page": 2, "heading": "第二章", "text": "每周运动150分钟"},
]


def run(backend, embedder, body, min_similarity=0.5):
    async def main():
        if backend == "memory":
            kb = MemoryKnowledgeBase(embedder=embedder, min_similarity=min_similarity)
            await kb.setup()
            await body(kb)
            return
        import psycopg

        schema = "t" + uuid.uuid4().hex[:10]
        async with await psycopg.AsyncConnection.connect(PG_URL, autocommit=True) as admin:
            await admin.execute(f"CREATE SCHEMA {schema}")
            try:
                kb = PgKnowledgeBase(f"{PG_URL}?options=-csearch_path%3D{schema},public", embedder=embedder, min_similarity=min_similarity)
                await kb.setup()
                try:
                    await body(kb)
                finally:
                    await kb.close()
            finally:
                await admin.execute(f"DROP SCHEMA {schema} CASCADE")

    asyncio.run(main())


@pytest.mark.parametrize("backend", BACKENDS)
def test_draft_is_not_searchable_until_published(backend):
    async def body(kb):
        doc = await kb.create_draft("指南", "《指南》", "2020", CHUNKS, "a.pdf")
        assert doc["status"] == "draft" and doc["chunk_count"] == 2 and doc["builtin"] is False
        assert await kb.search("盐") == []
        published = await kb.publish(doc["id"])
        assert published["status"] == "published" and published["published_at"]
        hits = await kb.search("盐")
        assert [h.text for h in hits] == ["每天吃盐不超过5克"]
        assert isinstance(hits[0], Chunk)
        assert hits[0].page == 1 and hits[0].source == "《指南》" and hits[0].title == "指南"
        assert hits[0].id == f"d{doc['id']}-0"
        assert hits[0].score > 0.5

    run(backend, fake_embedder, body)


@pytest.mark.parametrize("backend", BACKENDS)
def test_threshold_blocks_unrelated_query(backend):
    async def body(kb):
        doc = await kb.create_draft("指南", "", "", CHUNKS, "a.pdf")
        await kb.publish(doc["id"])
        assert await kb.search("西瓜") == []
        assert len(await kb.search("西瓜", min_similarity=0)) == 2

    run(backend, fake_embedder, body)


@pytest.mark.parametrize("backend", BACKENDS)
def test_edit_only_while_draft(backend):
    async def body(kb):
        doc = await kb.create_draft("指南", "", "", CHUNKS, "a.pdf")
        full = await kb.get_document(doc["id"])
        first, second = full["chunks"]
        await kb.update_chunk(doc["id"], first["id"], "每天吃盐不超过6克")
        await kb.delete_chunk(doc["id"], second["id"])
        full = await kb.get_document(doc["id"])
        assert [c["text"] for c in full["chunks"]] == ["每天吃盐不超过6克"]
        assert full["chunk_count"] == 1
        await kb.publish(doc["id"])
        assert (await kb.search("盐"))[0].text == "每天吃盐不超过6克"
        with pytest.raises(KnowledgeError):
            await kb.update_chunk(doc["id"], first["id"], "改")
        with pytest.raises(KnowledgeError):
            await kb.delete_chunk(doc["id"], first["id"])

    run(backend, fake_embedder, body)


@pytest.mark.parametrize("backend", BACKENDS)
def test_publishing_same_title_replaces_old_version(backend):
    async def body(kb):
        old = await kb.create_draft("指南", "", "2018", [CHUNKS[0]], "a.pdf")
        await kb.publish(old["id"])
        new = await kb.create_draft("指南", "", "2020", [{"seq": 0, "page": 1, "heading": "", "text": "新版：吃盐不超过4克"}], "b.pdf")
        assert (await kb.search("盐"))[0].text == CHUNKS[0]["text"]  # 新版没发布前旧版继续生效
        await kb.publish(new["id"])
        assert [h.text for h in await kb.search("盐")] == ["新版：吃盐不超过4克"]
        assert [d["version"] for d in await kb.list_documents()] == ["2020"]

    run(backend, fake_embedder, body)


@pytest.mark.parametrize("backend", BACKENDS)
def test_publish_failure_keeps_draft_and_old_data(backend):
    async def body(kb):
        good = await kb.create_draft("指南", "", "2018", [CHUNKS[0]], "a.pdf")
        await kb.publish(good["id"])
        new = await kb.create_draft("指南", "", "2020", [CHUNKS[1]], "b.pdf")
        kb.embedder = broken_embedder
        with pytest.raises(KnowledgeError):
            await kb.publish(new["id"])
        kb.embedder = fake_embedder
        assert (await kb.get_document(new["id"]))["status"] == "draft"
        assert (await kb.search("盐"))[0].text == CHUNKS[0]["text"]
        assert (await kb.stats())["vectors"] == 1

    run(backend, fake_embedder, body)


@pytest.mark.parametrize("backend", BACKENDS)
def test_search_falls_back_to_bm25_when_embedding_fails(backend):
    async def body(kb):
        doc = await kb.create_draft("指南", "", "", CHUNKS, "a.pdf")
        await kb.publish(doc["id"])
        kb.embedder = broken_embedder
        assert (await kb.search("运动"))[0].text == CHUNKS[1]["text"]

    run(backend, fake_embedder, body)


@pytest.mark.parametrize("backend", BACKENDS)
def test_seed_builtin_is_incremental_and_protected(backend):
    calls = []

    def counting(texts):
        calls.append(len(texts))
        return fake_embedder(texts)

    async def body(kb):
        builtin = [Chunk("salt", "限盐", "《指南》", "每天吃盐不超过5克"), Chunk("sport", "运动", "《指南》", "每周运动150分钟")]
        await kb.seed_builtin(builtin)
        first = sum(calls)
        assert first == 2
        await kb.seed_builtin(builtin)
        assert sum(calls) == first  # 内容没变不重新计费
        await kb.seed_builtin([Chunk("salt", "限盐", "《指南》", "每天吃盐不超过4克")])
        assert sum(calls) == first + 1
        hits = await kb.search("盐")
        assert [h.id for h in hits] == ["salt"] and "4克" in hits[0].text
        assert await kb.search("运动") == []  # 文件删掉后 chunk 也删除
        docs = await kb.list_documents()
        assert all(d["builtin"] for d in docs)
        with pytest.raises(KnowledgeError):
            await kb.delete_document(docs[0]["id"])

    run(backend, counting, body)


@pytest.mark.parametrize("backend", BACKENDS)
def test_delete_document(backend):
    async def body(kb):
        doc = await kb.create_draft("指南", "", "", CHUNKS, "a.pdf")
        await kb.publish(doc["id"])
        await kb.delete_document(doc["id"])
        assert await kb.search("盐") == []
        assert await kb.list_documents() == []
        assert (await kb.stats())["vectors"] == 0

    run(backend, fake_embedder, body)


@pytest.mark.parametrize("backend", BACKENDS)
def test_stats_and_list_chunks_show_what_is_searchable(backend):
    async def body(kb):
        empty = await kb.stats()
        assert empty["vectors"] == 0 and empty["dimension"] is None
        assert empty["backend"] == ("memory" if backend == "memory" else "pgvector")
        assert empty["min_similarity"] == 0.5
        draft = await kb.create_draft("草稿", "", "", [CHUNKS[1]], "d.pdf")
        doc = await kb.create_draft("指南", "《指南》", "2020", CHUNKS, "a.pdf")
        await kb.publish(doc["id"])
        stats = await kb.stats()
        assert stats["documents"] == 2 and stats["published_documents"] == 1 and stats["draft_documents"] == 1
        assert stats["chunks"] == 3 and stats["vectors"] == 2 and stats["dimension"] == 6

        listed = await kb.list_chunks()
        assert listed["total"] == 2  # 草稿不算
        row = listed["chunks"][0]
        assert row["id"] == f"d{doc['id']}-0" and row["document_id"] == doc["id"] and row["document_title"] == "指南"
        assert row["source"] == "《指南》" and row["page"] == 1 and row["heading"] == "第一章" and row["text"] == CHUNKS[0]["text"]
        assert row["dimension"] == 6
        assert row["vector_preview"] == pytest.approx([1, 0, 0, 0, 0, 0.01], abs=0.001)

        assert (await kb.list_chunks(q="运动"))["total"] == 1
        assert (await kb.list_chunks(document_id=draft["id"]))["total"] == 0
        paged = await kb.list_chunks(limit=1, offset=1)
        assert paged["total"] == 2 and [c["text"] for c in paged["chunks"]] == [CHUNKS[1]["text"]]

    run(backend, fake_embedder, body)


def test_preview_is_limited_to_eight_dimensions():
    def long_vectors(texts):
        return [[0.123456] * 20 for _ in texts]

    async def body(kb):
        doc = await kb.create_draft("指南", "", "", [CHUNKS[0]], "a.pdf")
        await kb.publish(doc["id"])
        row = (await kb.list_chunks())["chunks"][0]
        assert row["dimension"] == 20
        assert row["vector_preview"] == [0.1235] * 8

    run("memory", long_vectors, body)


def test_no_embedder_uses_bm25_and_has_no_vectors():
    async def body(kb):
        doc = await kb.create_draft("指南", "", "", CHUNKS, "a.pdf")
        await kb.publish(doc["id"])
        assert (await kb.search("每周运动"))[0].text == CHUNKS[1]["text"]
        row = (await kb.list_chunks())["chunks"][0]
        assert row["dimension"] is None and row["vector_preview"] is None
        assert (await kb.stats())["vectors"] == 0

    run("memory", None, body)
