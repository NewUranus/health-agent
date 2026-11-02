# 知识库后台规格

后台页面 `/admin`：上传 PDF → 解析成草稿 → 人工检查修改 → 发布（生成向量、写入向量库、进入检索），并能查看向量库内容。问答检索同时改成"向量优先 + 相关性门槛"。

优先级高于 `docs/DESIGN.md`、`docs/DESIGN_V2.md` 中的检索章节。

## 解析（app/ingest.py，纯函数）

```python
class IngestError(ValueError): ...   # message 是给用户看的中文

def parse_pdf(data: bytes) -> list[dict]        # [{"page": 1, "text": "..."}]，页码从 1 开始
def chunk_pages(pages: list[dict], max_chars: int = 500, overlap: int = 50) -> list[dict]
    # [{"seq": 0, "page": 1, "heading": "第一章 总则", "text": "..."}]，seq 从 0 递增
```

`parse_pdf` 用 PyMuPDF（`import pymupdf`）：

- 不是 PDF 或打不开：`IngestError("文件不是有效的 PDF。")`
- 加密：`IngestError("PDF 已加密，请先解除密码。")`
- 超过 500 页：`IngestError("PDF 超过 500 页，请拆分后上传。")`
- 平均每页少于 20 个非空白字符：`IngestError("没有提取到文字，可能是扫描版，目前不支持。")`

`chunk_pages`：

1. 去噪：出现在超过一半页面上的相同行（至少 3 页才判断）视为页眉页脚删除；只含页码的行删除（`12`、`第 12 页`、`- 12 -`）
2. 标题行以 `^第.+[章节]`、`^\d+(\.\d+)*\s`、`^[一二三四五六七八九十]+、` 开头，且长度不超过 40。遇到标题就开始新块，块的 `heading` 取最近的标题
3. 按行累积到 `max_chars` 就切块；单行超长时硬切；相邻硬切块之间重叠 `overlap` 个字符
4. 块的 `page` 是这块第一行所在的页
5. 空白块丢弃。块文本包含标题行

## 知识库（app/knowledge.py）

两个实现，接口相同，都是 async：`MemoryKnowledgeBase`（未配置 `DATABASE_URL`）和 `PgKnowledgeBase`（配置了，用 pgvector）。构造参数 `embedder`：`list[str] -> list[list[float]]`（同步，内部用 `asyncio.to_thread`，每批最多 10 条）或 `None`。另有 `min_similarity`，默认读环境变量 `RAG_MIN_SIMILARITY`，缺省 `0.44`。

| 方法 | 行为 |
|---|---|
| `setup()` | 建表；Pg 执行 `CREATE EXTENSION IF NOT EXISTS vector` |
| `seed_builtin(chunks)` | 把 `corpus/*.md` 作为内置文档（`builtin=True`，chunk id 用文件名）。内容哈希没变的不重新向量化。已不存在的文件对应的 chunk 删除 |
| `create_draft(title, source, version, chunks, filename)` | 建草稿，返回文档 dict |
| `list_documents()` / `get_document(id)` | 文档 dict，`get` 带 `chunks` |
| `update_chunk(doc_id, chunk_id, text)` / `delete_chunk(doc_id, chunk_id)` | 只允许改草稿，否则 `KnowledgeError` |
| `publish(doc_id)` | 为全部 chunk 生成向量，同一事务里：写入向量、状态改 `published`、**删除同标题的旧已发布文档**。向量生成失败时文档保持草稿，向量库不变，抛 `KnowledgeError` |
| `delete_document(doc_id)` | 删除文档和 chunk。内置文档不能删，抛 `KnowledgeError` |
| `search(query, k=4, min_similarity=None)` | 只查已发布。见下 |
| `stats()` | 见"查看向量库" |
| `list_chunks(document_id=None, q=None, limit=50, offset=0)` | 见"查看向量库" |

文档 dict：`id`、`title`、`source`、`version`、`filename`、`status`（`draft`/`published`）、`builtin`、`chunk_count`、`created_at`、`published_at`。

chunk id 必须匹配 `[\w-]+`，上传文档用 `d{文档id}-{seq}`（引用格式 `[cite:d3-12]` 依赖它）。

### search

有 embedder：`expand_query` 后向量化，按余弦相似度排序，过滤掉低于门槛的，返回前 k 条。Pg 用 `embedding <=> %s::vector` 查询。**没有混合检索**，评测里 RRF 比纯向量差（见 `eval/run_rag_eval.py`）。

向量化调用失败，或没有 embedder（Mock 模式）：退回 BM25（复用 `app/rag.py` 的 `tokenize`），只在已发布 chunk 上查，不套用门槛，记 warning 日志。

返回 `app.rag.Chunk`，增加字段 `page: int | None = None`。`score` 是相似度（BM25 时是 BM25 分）。

Pg 的 `embedding` 列类型是不带维度的 `vector`（数据量小，精确扫描即可，不建索引）。向量用 `'[...]'` 字符串加 `::vector` 传参，不增加 pip 依赖。

## 接入 Agent

- `HealthAgent` 启动时建知识库、`setup()`、`seed_builtin()`。`build_graph` 接收知识库，替换原来的 `retriever`
- `retrieve` 节点和 `answer` 节点改用 `await kb.search(...)`。事件 `chunks` 带 `page`
- `retrieve` 节点调用时 `min_similarity=0`（查询是代码拼的通用词，不套门槛）
- `answer` 节点：检索结果为空时**不调模型、不调工具**，回复 `资料库里没有找到相关依据，无法回答这个问题，建议咨询医生。` 加免责声明，事件数据 `no_evidence: true`
- `answer.md`、`advise.md` 提示词补一条并把版本加一：`资料针对的人群或场景与问题不符时（如问题问儿童、孕妇而资料讲普通成人），明确说明资料未覆盖，不要套用。`
- 前端来源卡片有 `page` 时显示 `第 N 页`

## HTTP（app/main.py）

全部 `/api/admin/*` 需要请求头 `Authorization: Bearer <密码>`，密码来自环境变量 `ADMIN_PASSWORD`，**每次请求时读取**，用 `hmac.compare_digest` 比较。

- 未配置 `ADMIN_PASSWORD`：一律 503，`{"detail": "后台未启用。"}`
- 密码错误或缺失：401

| 接口 | 说明 |
|---|---|
| `GET /admin` | 返回 `web/index.html`，同样 `Cache-Control: no-cache`。页面本身不需要密码，数据接口才需要 |
| `POST /api/admin/documents` | multipart：`file`、`title`、`source`、`version`（后两个可选，title 必填）。超过 20MB 返回 413。`IngestError` 返回 400，`detail` 是错误文案。没切出 chunk 返回 400。成功 201 返回带 chunks 的文档 |
| `GET /api/admin/documents` | `{"documents": [...]}` |
| `GET /api/admin/documents/{id}` | 带 chunks；不存在 404 |
| `PATCH /api/admin/documents/{id}/chunks/{chunk_id}` | body `{"text": "..."}`。空文本 400；已发布 409 |
| `DELETE /api/admin/documents/{id}/chunks/{chunk_id}` | 已发布 409 |
| `POST /api/admin/documents/{id}/publish` | 成功返回文档。失败 502，`detail` 是错误文案，文档保持草稿 |
| `DELETE /api/admin/documents/{id}` | 内置文档 409 |
| `GET /api/admin/stats` | 见下 |
| `GET /api/admin/chunks` | 查询参数 `document_id`、`q`、`limit`（默认 50，最大 200）、`offset` |
| `GET /api/admin/search?q=&k=` | 用真实检索逻辑试搜，返回 `{"results": [{id,title,source,page,text,score}], "min_similarity": 0.44}` |

### 查看向量库

`stats()` 返回：

```json
{"backend": "pgvector", "embedding_model": "qwen3.7-text-embedding", "dimension": 1024,
 "documents": 3, "published_documents": 2, "draft_documents": 1,
 "chunks": 158, "vectors": 142, "min_similarity": 0.44}
```

`backend` 是 `pgvector` 或 `memory`。`vectors` 是有向量的已发布 chunk 数；`dimension` 没有向量时为 `null`。`embedding_model` 没有配置时为 `null`。

`list_chunks()` 只列**已发布**的 chunk（也就是真正能被检索到的内容），返回：

```json
{"total": 142, "chunks": [{"id": "d3-12", "document_id": 3, "document_title": "...", "source": "...",
  "page": 12, "heading": "...", "text": "...", "dimension": 1024, "vector_preview": [0.0123, -0.0456, ...]}]}
```

`vector_preview` 是前 8 维，保留 4 位小数；没有向量时 `dimension` 和 `vector_preview` 都是 `null`。`q` 按文本子串过滤（大小写不敏感），`total` 是过滤后的总数。按文档、seq 排序。

## 前端

`location.pathname === "/admin"` 时渲染 `AdminApp`（`src/admin/AdminApp.tsx`，默认导出），否则渲染原来的 `App`。不引入路由库。

- **登录**：输入密码，存 `sessionStorage` 的 `healthpilot-admin`（读写 try/catch）。请求 401 时清除并回到登录页，提示 `密码错误`。503 提示 `后台未启用`。不要把密码放进 URL 或日志
- **三个标签页**（`role="tab"`）：`文档`、`向量库`、`检索测试`
- **文档**：上传表单（文件、标题、来源、版本）；文档列表显示标题、状态（`草稿`/`已发布`/`内置`）、块数。上传后打开草稿详情：每块显示 `第 N 页`、标题和可编辑文本，有 `保存`、`删除`；顶部提示 `请核对表格和数字后再发布` 和 `发布` 按钮。发布时按钮显示 `发布中…`。内置文档不显示删除按钮。删除文档前 `window.confirm`
- **向量库**：顶部显示 stats（后端、模型、维度、文档数、向量数、门槛）；搜索框（`q`）和文档筛选；列表每条显示 id、文档名、页码、文本、`1024 维` 和可展开的向量预览；分页按钮 `上一页`、`下一页`，显示 `共 N 条`
- **检索测试**：输入问题，显示结果和相似度；没有结果时显示 `没有达到门槛的结果，问答会拒答`
- 所有文本作为文本节点渲染，不用 `dangerouslySetInnerHTML`。错误信息显示 `detail`
- 页面标注 `内容发布前请人工核对`

## 部署

- `docker-compose.yml` 的数据库镜像换成 `pgvector/pgvector:pg16`（设计方修改）
- `.env` 增加 `ADMIN_PASSWORD`
- 上传的 PDF 不落盘、不进 git，只保存解析后的文本
