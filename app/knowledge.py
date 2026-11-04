"""草稿、发布与可检索知识库；向量优先，失败时退回 BM25。"""
import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import re
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

from rank_bm25 import BM25Okapi

from app.rag import Chunk, tokenize
from app.terms import expand_query

logger = logging.getLogger(__name__)


class KnowledgeError(ValueError):
    """知识库业务错误，文案可展示给管理员。"""


def now():
    return datetime.now(timezone.utc).isoformat()


def content_hash(chunk):
    return hashlib.sha256(json.dumps([chunk.title, chunk.source, chunk.text, chunk.page], ensure_ascii=False).encode()).hexdigest()


def make_chunks(doc_id, chunks):
    rows = []
    for chunk in chunks:
        seq = chunk['seq']
        if not isinstance(seq, int) or seq < 0 or any(c['seq'] == seq for c in rows):
            raise KnowledgeError("片段序号必须是唯一的非负整数。")
        if not chunk['text'].strip():
            raise KnowledgeError("片段文本不能为空。")
        rows.append({"id": f"d{doc_id}-{seq}", "document_id": doc_id, "seq": seq,
                     "page": chunk.get("page"), "heading": chunk.get("heading", ""),
                     "text": chunk['text'], "embedding": None})
    return rows


def hit(row, score=0):
    return Chunk(row['id'], row['document_title'], row['source'], row['text'], float(score), row['page'])


def preview(row):
    vector = row['embedding']
    return {key: row[key] for key in ('id', 'document_id', 'document_title', 'source', 'page', 'heading', 'text')} | {
        'variant': row.get('variant', 'standard'), 'comparison_id': row.get('comparison_id', ''),
        'dimension': len(vector) if vector is not None else None,
        'vector_preview': [round(x, 4) for x in vector[:8]] if vector is not None else None}


def bm25(rows, query, k):
    if not rows or k <= 0:
        return []
    tokens = [tokenize(row['document_title'] + ' ' + row['text']) for row in rows]
    if not any(tokens):
        return []
    index = BM25Okapi(tokens)
    # 正值 IDF 适用于刚发布一两块的小语料，也避免常见词得到负分。
    frequencies = {}
    for terms in tokens:
        for term in set(terms):
            frequencies[term] = frequencies.get(term, 0) + 1
    index.idf = {term: math.log(1 + (len(rows) - count + 0.5) / (count + 0.5)) for term, count in frequencies.items()}
    scores = index.get_scores(tokenize(query))
    return [hit(rows[i], scores[i]) for i in sorted(range(len(rows)), key=lambda i: -scores[i]) if scores[i] > 0][:k]


class KnowledgeBase:
    backend = 'memory'

    def __init__(self, embedder=None, min_similarity=None):
        self.embedder = embedder
        self.min_similarity = float(os.getenv('RAG_MIN_SIMILARITY', '0.44')) if min_similarity is None else min_similarity
        self._query_vectors = OrderedDict()
        self._query_tasks = {}
        self._query_embedder = embedder

    async def create_draft(self, title, source, version, chunks, filename):
        documents = await self.create_drafts([{
            'title': title, 'source': source, 'version': version, 'chunks': chunks,
            'filename': filename, 'variant': 'standard', 'comparison_id': '',
        }])
        return documents[0]

    async def create_comparison(self, title, source, version, raw_chunks, clean_chunks, filename):
        """一次上传的两版原子写入同一个知识库，标记不参与向量化。"""
        comparison_id = uuid4().hex
        return await self.create_drafts([
            {'title': title, 'source': source, 'version': version, 'filename': filename,
             'variant': variant, 'comparison_id': comparison_id, 'chunks': chunks}
            for variant, chunks in [('raw', raw_chunks), ('clean', clean_chunks)]
        ])

    async def query_vector(self, query):
        """只缓存成功的查询向量，检索结果始终读取当前已发布内容。"""
        if self._query_embedder is not self.embedder:
            self._query_vectors.clear()
            self._query_tasks.clear()
            self._query_embedder = self.embedder
        if query in self._query_vectors:
            self._query_vectors.move_to_end(query)
            return list(self._query_vectors[query])
        task = self._query_tasks.get(query)
        if task is None:
            task = asyncio.create_task(self.embed([query]))
            self._query_tasks[query] = task

            def completed(done):
                # 被替换的模型任务不能污染新模型的缓存；失败留给下次重试。
                current = self._query_tasks.get(query) is done
                if current:
                    self._query_tasks.pop(query, None)
                if done.cancelled() or done.exception() is not None:
                    return
                if current:
                    self._query_vectors[query] = tuple(done.result()[0])
                    if len(self._query_vectors) > 256:
                        self._query_vectors.popitem(last=False)

            task.add_done_callback(completed)
        # 某个请求取消时，不取消其他请求仍在等待的向量计算。
        return list((await asyncio.shield(task))[0])

    async def embed(self, texts):
        if self.embedder is None:
            return [None] * len(texts)
        vectors = []
        for start in range(0, len(texts), 10):
            batch = texts[start:start + 10]
            generated = await asyncio.to_thread(self.embedder, batch)
            if len(generated) != len(batch):
                raise ValueError('向量数量与片段数量不一致')
            for vector in generated:
                vector = [float(x) for x in vector]
                if not vector or not all(math.isfinite(x) for x in vector) or not any(vector):
                    raise ValueError('向量无效')
                if vectors and len(vector) != len(vectors[0]):
                    raise ValueError('向量维度不一致')
                vectors.append(vector)
        return vectors

    async def publication_vectors(self, document):
        if not document['chunks']:
            raise KnowledgeError('文档没有可发布的片段。')
        try:
            return await self.embed([document['title'] + ' ' + c['text'] for c in document['chunks']])
        except Exception:
            raise KnowledgeError('向量生成失败，文档保持草稿。') from None

    async def search(self, query, k=4, min_similarity=None, *, document_id=None):
        if k <= 0:
            return []
        query = expand_query(query)
        if self.embedder is not None:
            try:
                vector = await self.query_vector(query)
            except Exception:
                logger.warning('向量查询失败，本次退回 BM25')
            else:
                threshold = self.min_similarity if min_similarity is None else min_similarity
                return await self.vector_search(vector, k, threshold, document_id=document_id)
        else:
            logger.warning('未配置向量模型，本次使用 BM25')
        return await asyncio.to_thread(bm25, await self.published_rows(document_id=document_id), query, k)

    async def list_chunks(self, document_id=None, q=None, limit=50, offset=0):
        rows = [r for r in await self.published_rows() if (document_id is None or r['document_id'] == document_id)
                and (not q or q.casefold() in r['text'].casefold())]
        return {'total': len(rows), 'chunks': [preview(r) for r in rows[max(0, offset):max(0, offset) + min(200, max(0, limit))]]}

    async def compare(self, document_id, query, k=4):
        document = await self.get_document(document_id)
        comparison_id = document.get('comparison_id')
        pair = [d for d in await self.list_documents()
                if comparison_id and d.get('comparison_id') == comparison_id and d['status'] == 'published']
        if len(pair) != 2 or {d.get('variant') for d in pair} != {'raw', 'clean'}:
            raise KnowledgeError('请先发布同次上传的原始版和清洗版。')
        query = expand_query(query)
        vector = None
        if self.embedder is not None:
            try:
                vector = await self.query_vector(query)
            except Exception:
                logger.warning('对比查询向量生成失败，两版均退回 BM25')
        results = []
        for doc in sorted(pair, key=lambda d: d['variant'] != 'raw'):
            if vector is not None:
                hits = await self.vector_search(vector, k, self.min_similarity, document_id=doc['id'])
            else:
                hits = await asyncio.to_thread(bm25, await self.published_rows(document_id=doc['id']), query, k)
            results.append({'document_id': doc['id'], 'title': doc['title'], 'variant': doc['variant'],
                            'results': [vars(hit) for hit in hits]})
        return {'groups': results, 'mode': 'vector' if vector is not None else 'bm25',
                'min_similarity': self.min_similarity}

    async def stats(self):
        docs = await self.list_documents()
        rows = await self.published_rows()
        vectors = [r['embedding'] for r in rows if r['embedding'] is not None]
        published = sum(d['status'] == 'published' for d in docs)
        return {'backend': self.backend, 'embedding_model': os.getenv('QWEN_EMBEDDING_MODEL') or None,
                'dimension': len(vectors[0]) if vectors else None, 'documents': len(docs),
                'published_documents': published, 'draft_documents': len(docs) - published,
                'chunks': sum(d['chunk_count'] for d in docs), 'vectors': len(vectors), 'min_similarity': self.min_similarity}


class MemoryKnowledgeBase(KnowledgeBase):
    def __init__(self, embedder=None, min_similarity=None):
        super().__init__(embedder, min_similarity)
        self.documents = {}
        self.next_id = 1
        self.lock = asyncio.Lock()

    async def setup(self):
        pass

    async def close(self):
        pass

    def require(self, doc_id, draft=False):
        document = self.documents.get(doc_id)
        if document is None:
            raise KnowledgeError('文档不存在。')
        if draft and document['status'] != 'draft':
            raise KnowledgeError('只允许修改草稿。')
        return document

    def public_document(self, document, full=False):
        result = {key: value for key, value in document.items() if key not in ('chunks', 'content_hash')}
        result['chunk_count'] = len(document['chunks'])
        if full:
            result['chunks'] = [{k: v for k, v in row.items() if k != 'embedding'} for row in document['chunks']]
        return copy.deepcopy(result)

    async def create_drafts(self, drafts):
        async with self.lock:
            documents = []
            for offset, draft in enumerate(drafts):
                doc_id = self.next_id + offset
                rows = make_chunks(doc_id, draft['chunks'])
                documents.append({**draft, 'id': doc_id, 'status': 'draft', 'builtin': False,
                                  'created_at': now(), 'published_at': None, 'chunks': rows})
            self.documents.update({d['id']: d for d in documents})
            self.next_id += len(documents)
            return [self.public_document(d, True) for d in documents]

    async def list_documents(self):
        return [self.public_document(d) for d in self.documents.values()]

    async def get_document(self, doc_id):
        return self.public_document(self.require(doc_id), True)

    async def update_chunk(self, doc_id, chunk_id, text):
        if not text.strip():
            raise KnowledgeError('片段文本不能为空。')
        async with self.lock:
            document = self.require(doc_id, True)
            row = next((c for c in document['chunks'] if c['id'] == chunk_id), None)
            if row is None:
                raise KnowledgeError('片段不存在。')
            row['text'] = text
            return self.public_document(document, True)

    async def delete_chunk(self, doc_id, chunk_id):
        async with self.lock:
            document = self.require(doc_id, True)
            if not any(c['id'] == chunk_id for c in document['chunks']):
                raise KnowledgeError('片段不存在。')
            document['chunks'] = [c for c in document['chunks'] if c['id'] != chunk_id]
            return self.public_document(document, True)

    async def publish(self, doc_id):
        async with self.lock:
            document = self.require(doc_id, True)
            vectors = await self.publication_vectors(document)
            for chunk, vector in zip(document['chunks'], vectors):
                chunk['embedding'] = vector
            document.update(status='published', published_at=now())
            self.documents = {i: d for i, d in self.documents.items()
                              if i == doc_id or d['title'] != document['title'] or d['status'] != 'published'
                              or d.get('variant', 'standard') != document.get('variant', 'standard')}
            return self.public_document(document, True)

    async def delete_document(self, doc_id):
        async with self.lock:
            if self.require(doc_id)['builtin']:
                raise KnowledgeError('内置文档不能删除。')
            del self.documents[doc_id]

    async def seed_builtin(self, chunks):
        async with self.lock:
            old = {d['chunks'][0]['id']: d for d in self.documents.values() if d['builtin']}
            changed = [c for c in chunks if c.id not in old or old[c.id].get('content_hash') != content_hash(c)]
            if any(not re.fullmatch(r'[\w-]+', c.id) for c in chunks):
                raise KnowledgeError('内置片段标识无效。')
            try:
                vectors = await self.embed([c.title + ' ' + c.text for c in changed])
            except Exception:
                raise KnowledgeError('内置资料向量生成失败。') from None
            for chunk, vector in zip(changed, vectors):
                doc_id = old[chunk.id]['id'] if chunk.id in old else self.next_id
                if chunk.id not in old:
                    self.next_id += 1
                self.documents[doc_id] = {'id': doc_id, 'title': chunk.title, 'source': chunk.source, 'version': '',
                    'filename': chunk.id + '.md', 'status': 'published', 'builtin': True, 'content_hash': content_hash(chunk),
                    'created_at': old[chunk.id]['created_at'] if chunk.id in old else now(), 'published_at': now(),
                    'chunks': [{'id': chunk.id, 'document_id': doc_id, 'seq': 0, 'page': chunk.page,
                                'heading': chunk.title, 'text': chunk.text, 'embedding': vector}]}
            ids = {c.id for c in chunks}
            for cid, document in old.items():
                if cid not in ids:
                    del self.documents[document['id']]

    async def published_rows(self, document_id=None):
        return [copy.deepcopy(c) | {'document_title': d['title'], 'source': d['source'],
                                   'variant': d.get('variant', 'standard'), 'comparison_id': d.get('comparison_id', '')}
                for d in sorted(self.documents.values(), key=lambda d: d['id'])
                if d['status'] == 'published' and (document_id is None or d['id'] == document_id)
                for c in sorted(d['chunks'], key=lambda c: c['seq'])]

    async def vector_search(self, vector, k, threshold, document_id=None):
        scored = []
        for row in await self.published_rows(document_id=document_id):
            other = row['embedding']
            if other is None or len(other) != len(vector):
                continue
            norm = math.sqrt(sum(x*x for x in vector) * sum(x*x for x in other))
            score = sum(a*b for a, b in zip(vector, other)) / norm if norm else 0
            if score >= threshold:
                scored.append(hit(row, score))
        return sorted(scored, key=lambda c: -c.score)[:k]


class PgKnowledgeBase(KnowledgeBase):
    backend = 'pgvector'

    def __init__(self, url, embedder=None, min_similarity=None):
        super().__init__(embedder, min_similarity)
        self.url = url

    @asynccontextmanager
    async def connection(self):
        import psycopg
        from psycopg.rows import dict_row
        async with await psycopg.AsyncConnection.connect(self.url, row_factory=dict_row) as connection:
            yield connection

    async def setup(self):
        from app.database import schema_lock
        async with schema_lock(self.url), self.connection() as conn:
            await conn.execute('CREATE EXTENSION IF NOT EXISTS vector')
            await conn.execute('''CREATE TABLE IF NOT EXISTS knowledge_documents (
                id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL, version TEXT NOT NULL,
                filename TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('draft', 'published')),
                builtin BOOLEAN NOT NULL DEFAULT FALSE, content_hash TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(), published_at TIMESTAMPTZ)''')
            await conn.execute("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS variant TEXT NOT NULL DEFAULT 'standard'")
            await conn.execute("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS comparison_id TEXT NOT NULL DEFAULT ''")
            await conn.execute('''CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id TEXT PRIMARY KEY CHECK (id ~ '^[[:alnum:]_-]+$'),
                document_id BIGINT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL, page INTEGER, heading TEXT NOT NULL, text TEXT NOT NULL,
                embedding vector, UNIQUE(document_id, seq))''')

    async def close(self):
        # 连接按操作打开并由上下文关闭，没有常驻连接。
        pass

    async def document(self, conn, doc_id, draft=False):
        row = await (await conn.execute('SELECT * FROM knowledge_documents WHERE id = %s', (doc_id,))).fetchone()
        if row is None:
            raise KnowledgeError('文档不存在。')
        if draft and row['status'] != 'draft':
            raise KnowledgeError('只允许修改草稿。')
        row.pop('content_hash', None)
        for key in ('created_at', 'published_at'):
            row[key] = row[key].isoformat() if row[key] else None
        row['chunks'] = await (await conn.execute('''SELECT id, document_id, seq, page, heading, text
            FROM knowledge_chunks WHERE document_id = %s ORDER BY seq''', (doc_id,))).fetchall()
        row['chunk_count'] = len(row['chunks'])
        return row

    async def lock_writes(self, conn):
        # 跨进程串行化写入，防止同标题并发发布或编辑覆盖正在生成的向量。
        await conn.execute('LOCK TABLE knowledge_documents IN SHARE ROW EXCLUSIVE MODE')

    async def create_drafts(self, drafts):
        async with self.connection() as conn:
            await self.lock_writes(conn)
            documents = []
            for draft in drafts:
                row = await (await conn.execute('''INSERT INTO knowledge_documents
                    (title, source, version, filename, variant, comparison_id, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'draft') RETURNING id''',
                    tuple(draft[key] for key in ('title', 'source', 'version', 'filename', 'variant', 'comparison_id')))).fetchone()
                for chunk in make_chunks(row['id'], draft['chunks']):
                    await self.insert_chunk(conn, chunk)
                documents.append(await self.document(conn, row['id']))
            return documents

    async def insert_chunk(self, conn, chunk):
        vector = json.dumps(chunk['embedding']) if chunk['embedding'] is not None else None
        await conn.execute('''INSERT INTO knowledge_chunks (id, document_id, seq, page, heading, text, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s::vector)''',
            (chunk['id'], chunk['document_id'], chunk['seq'], chunk['page'], chunk['heading'], chunk['text'], vector))

    async def get_document(self, doc_id):
        async with self.connection() as conn:
            return await self.document(conn, doc_id)

    async def list_documents(self):
        async with self.connection() as conn:
            rows = await (await conn.execute('''SELECT d.id, d.title, d.source, d.version, d.filename,
                d.status, d.builtin, d.variant, d.comparison_id, d.created_at, d.published_at, COUNT(c.id) AS chunk_count
                FROM knowledge_documents d LEFT JOIN knowledge_chunks c ON c.document_id = d.id
                GROUP BY d.id ORDER BY d.id''')).fetchall()
            for row in rows:
                for key in ('created_at', 'published_at'):
                    row[key] = row[key].isoformat() if row[key] else None
            return rows

    async def update_chunk(self, doc_id, chunk_id, text):
        if not text.strip():
            raise KnowledgeError('片段文本不能为空。')
        async with self.connection() as conn:
            await self.lock_writes(conn)
            await self.document(conn, doc_id, True)
            cursor = await conn.execute('UPDATE knowledge_chunks SET text = %s WHERE document_id = %s AND id = %s', (text, doc_id, chunk_id))
            if not cursor.rowcount:
                raise KnowledgeError('片段不存在。')
            return await self.document(conn, doc_id)

    async def delete_chunk(self, doc_id, chunk_id):
        async with self.connection() as conn:
            await self.lock_writes(conn)
            await self.document(conn, doc_id, True)
            cursor = await conn.execute('DELETE FROM knowledge_chunks WHERE document_id = %s AND id = %s', (doc_id, chunk_id))
            if not cursor.rowcount:
                raise KnowledgeError('片段不存在。')
            return await self.document(conn, doc_id)

    async def publish(self, doc_id):
        async with self.connection() as conn:
            await self.lock_writes(conn)
            document = await self.document(conn, doc_id, True)
            vectors = await self.publication_vectors(document)
            try:
                for chunk, vector in zip(document['chunks'], vectors):
                    await conn.execute('UPDATE knowledge_chunks SET embedding = %s::vector WHERE id = %s',
                                       (json.dumps(vector) if vector is not None else None, chunk['id']))
                await conn.execute("UPDATE knowledge_documents SET status = 'published', published_at = now() WHERE id = %s", (doc_id,))
                await conn.execute("DELETE FROM knowledge_documents WHERE title = %s AND variant = %s AND status = 'published' AND id <> %s",
                                   (document['title'], document['variant'], doc_id))
                return await self.document(conn, doc_id)
            except Exception:
                raise KnowledgeError('发布失败，文档保持草稿。') from None

    async def delete_document(self, doc_id):
        async with self.connection() as conn:
            await self.lock_writes(conn)
            if (await self.document(conn, doc_id))['builtin']:
                raise KnowledgeError('内置文档不能删除。')
            await conn.execute('DELETE FROM knowledge_documents WHERE id = %s', (doc_id,))

    async def seed_builtin(self, chunks):
        if any(not re.fullmatch(r'[\w-]+', c.id) for c in chunks):
            raise KnowledgeError('内置片段标识无效。')
        async with self.connection() as conn:
            await self.lock_writes(conn)
            existing = await (await conn.execute('''SELECT d.id, d.content_hash, c.id AS chunk_id
                FROM knowledge_documents d JOIN knowledge_chunks c ON c.document_id = d.id WHERE d.builtin''')).fetchall()
            old = {r['chunk_id']: r for r in existing}
            changed = [c for c in chunks if c.id not in old or old[c.id]['content_hash'] != content_hash(c)]
            try:
                vectors = await self.embed([c.title + ' ' + c.text for c in changed])
            except Exception:
                raise KnowledgeError('内置资料向量生成失败。') from None
            for chunk, vector in zip(changed, vectors):
                if chunk.id in old:
                    doc_id = old[chunk.id]['id']
                    await conn.execute('''UPDATE knowledge_documents SET title = %s, source = %s,
                        content_hash = %s, published_at = now() WHERE id = %s''', (chunk.title, chunk.source, content_hash(chunk), doc_id))
                    await conn.execute('DELETE FROM knowledge_chunks WHERE document_id = %s', (doc_id,))
                else:
                    row = await (await conn.execute('''INSERT INTO knowledge_documents
                        (title, source, version, filename, status, builtin, content_hash, published_at)
                        VALUES (%s, %s, '', %s, 'published', TRUE, %s, now()) RETURNING id''',
                        (chunk.title, chunk.source, chunk.id + '.md', content_hash(chunk)))).fetchone()
                    doc_id = row['id']
                await self.insert_chunk(conn, {'id': chunk.id, 'document_id': doc_id, 'seq': 0,
                    'page': chunk.page, 'heading': chunk.title, 'text': chunk.text, 'embedding': vector})
            ids = {c.id for c in chunks}
            for cid, row in old.items():
                if cid not in ids:
                    await conn.execute('DELETE FROM knowledge_documents WHERE id = %s', (row['id'],))

    async def published_rows(self, document_id=None):
        async with self.connection() as conn:
            rows = await (await conn.execute('''SELECT c.id, c.document_id, c.seq, c.page, c.heading, c.text,
                c.embedding::text AS embedding, d.title AS document_title, d.source, d.variant, d.comparison_id
                FROM knowledge_chunks c JOIN knowledge_documents d ON d.id = c.document_id
                WHERE d.status = 'published' AND (%s::bigint IS NULL OR c.document_id = %s)
                ORDER BY d.id, c.seq''', (document_id, document_id))).fetchall()
            for row in rows:
                row['embedding'] = json.loads(row['embedding']) if row['embedding'] is not None else None
            return rows

    async def vector_search(self, vector, k, threshold, document_id=None):
        async with self.connection() as conn:
            rows = await (await conn.execute('''SELECT * FROM (
                SELECT c.id, c.page, c.text, d.title AS document_title, d.source,
                    1 - (c.embedding <=> %s::vector) AS score
                FROM knowledge_chunks c JOIN knowledge_documents d ON d.id = c.document_id
                WHERE d.status = 'published' AND c.embedding IS NOT NULL AND vector_dims(c.embedding) = %s
                    AND (%s::bigint IS NULL OR c.document_id = %s)
                ) ranked WHERE score >= %s ORDER BY score DESC, id LIMIT %s''',
                (json.dumps(vector), len(vector), document_id, document_id, threshold, k))).fetchall()
            return [hit(row, row['score']) for row in rows]
