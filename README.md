# HealthPilot

一个心血管健康评估 Agent 的练习项目。用户在网页上用中文描述健康情况，系统追问缺失信息，计算 10 年心血管病风险，再根据指南摘要给出带引用的生活方式建议。

这是技术演示，没有真实用户，也没有经过医学人员审核，不构成医疗建议。

**在线演示**：https://47-250-192-131.sslip.io （服务器配置较低，偶尔可能较慢；请只填写虚拟数据）

## 功能

- **多轮问诊**：缺信息时追问，同一会话不用重复已说过的内容
- **急症护栏**：胸痛、呼吸困难、卒中症状等用关键词规则识别，命中后直接提示拨打 120
- **风险评估**：Framingham 2008 公式，通过 MCP 工具调用
- **干预模拟和轨迹推演**：可以在对话里问"戒烟后风险降多少"，也可以在页面上拖动血压、胆固醇滑块或勾选戒烟，实时看风险变化；另外显示 5 年、10 年后的风险。都只是代入公式计算，不调用大模型，也不修改档案
- **带引用的建议**：向量检索加相关性门槛，低于门槛直接拒答；生成后检查引用是否有效、是否出现药物剂量，不通过就重试，仍不通过就拒答
- **知识库后台**（`/admin`）：上传文字版 PDF，解析成草稿，人工修改后发布进向量库；可以查看向量库内容和试搜
- **随访**：评估后记录下次复查日期，到期打开页面时提醒；复查时用 LangGraph `interrupt` 多轮询问血压、用药或生活方式、吸烟和症状，再与历次风险对比。中途提到急症会立即中止并提示拨打 120
- **网页界面**：实时显示每一步的节点、工具调用和耗时
- **随访记录**：页面上用折线图显示历次风险，并列出每次的血压、依从情况和症状

## 技术栈

| 部分 | 技术 |
|---|---|
| 前端 | React + TypeScript + Vite，Vitest 测试；用 `fetch` 读取 SSE 流式事件 |
| 后端 | Python 3.12、FastAPI、Uvicorn，SSE 流式接口 |
| Agent 编排 | LangGraph（主图 + 子图、checkpointer、Store） |
| 工具 | MCP Python SDK 2.x，stdio 子进程；Function Calling 循环 |
| 模型 | 千问（OpenAI 兼容接口，通过 langchain-openai 调用） |
| 检索 | 向量检索（千问 embedding + pgvector），向量不可用时退回 BM25（rank-bm25 + jieba） |
| 文档解析 | PyMuPDF，只支持文字版 PDF |
| 存储 | PostgreSQL + pgvector（会话、长期记忆、知识库），未配置时用内存 |
| 部署 | Docker Compose、Caddy（HTTPS），阿里云轻量服务器 |
| CI | GitHub Actions（测试和 Mock 评测） |

## 结构

用 LangGraph 编排，每条消息的流程是：

```
急症检查 → 整理档案 → supervisor 路由 → 问诊 / 评估→建议→随访 / 模拟 / 复查 / 问答
```

路由是确定性规则，不由模型决定。模型只负责补充抽取信息、撰写建议和回答问题，风险计算、急症判断和引用校验都是普通代码。

| 目录 | 内容 |
|---|---|
| `app/` | FastAPI 接口、状态图、检索、校验、记忆 |
| `mcp_server/` | MCP 工具服务（stdio） |
| `corpus/` | 16 段指南摘要 |
| `frontend/` | 前端源码（React + TypeScript） |
| `web/` | 前端构建结果，由 FastAPI 托管 |
| `eval/` | 评测脚本和 26 条用例 |
| `docs/` | 设计文档、部署说明 |

## 运行

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。

不调用模型（用模板生成回复，不需要 key）：

```bash
uv sync
HEALTH_AGENT_MOCK=1 uv run uvicorn app.main:app --port 8000
```

使用真实模型，需要在 `.env` 里配置千问的 OpenAI 兼容接口：

```
QWEN_API_KEY=...
QWEN_BASE_URL=...
QWEN_MODEL=qwen3.6-plus
QWEN_EMBEDDING_MODEL=qwen3.7-text-embedding   # 可选，不填只用 BM25
```

```bash
uv run uvicorn app.main:app --port 8000
```

然后打开 http://localhost:8000 。真实模式现在默认要求账号登录，首次运行需按下文创建账号；本地 HTTP 调试需设置 `HEALTH_AGENT_COOKIE_SECURE=0`。模型调用失败时会退回模板回复，并在轨迹里提示。

改前端时在 `frontend/` 下运行 `npm run dev`；改完运行 `npm run build`，并把 `web/` 一起提交。

用 Docker 部署（含 Postgres、HTTPS 和访问口令）见 [docs/DEPLOY.md](docs/DEPLOY.md)。

## API V1

当前 HTTP API 明确为 **V1**，OpenAPI 文档版本为 `1.0.0`，当前 Agent 实现标记为 `v1`。

- 正式入口：`/api/v1/...`，例如 `POST /api/v1/chat`、`GET /api/v1/health`、`POST /api/v1/tools/cvd_risk`、`GET /api/v1/admin/documents`。
- 兼容入口：现有 `/api/...` 固定对应 V1。两个入口共用处理函数、鉴权、参数校验和数据，不做 HTTP 重定向；登录后同一账号、`thread_id` 可以跨入口继续同一会话。真实模式的旧客户端现在也必须先登录，并在写请求中携带 `X-HealthPilot-Request: 1`；不能继续依赖客户端自报身份。
- V1 响应头包含 `X-API-Version: v1` 和 `X-Agent-Version: v1`，聊天 SSE 响应也包含这两个头。现有 JSON 结构和 SSE 事件格式保持兼容。
- `/docs` 和 `/openapi.json` 展示 V1 正式接口及兼容入口；后台两组路径均受 `ADMIN_PASSWORD` 控制，登录模式未配置此项时拒绝访问。`/`、`/admin` 和 `/static` 是页面资源路径，不加 API 版本。
- 未来有不兼容的接口变化时，应另建 `/api/v2/...`，保留 V1。当前仅有一个 Agent 实现，版本标识不代表已经提供旧模型快照、多版本 Agent 选择或检查点迁移；`/api/v2/...` 当前返回 404。

无需模型的状态检查：

```bash
curl -i http://localhost:8000/api/v1/health
```

## 本地验收与第一批基础建设

完整的 51 项目标、依赖、验收条件及进度见 [验收矩阵](ACCEPTANCE_MATRIX.md)。第一批实现用户登录、数据隔离、请求编号、标准错误及基础审计；尚未达到企业级。

### 创建账号与启动

密码交互输入，不传入命令行参数，不提供默认账号或默认密码：

```bash
uv run python -m app.auth create-user alice
# 重置密码同时注销该账号的所有现有会话
uv run python -m app.auth reset-password alice
```

- 账号为 3–64 位字母、数字、点、横线或下划线，按小写存储；密码 12–256 字符，使用加盐 scrypt 哈希。
- 配置 `DATABASE_URL` 时，账号、会话、线程归属与基础审计使用同一 PostgreSQL 的 `hp_*` 表；没有配置时，使用 `runs/auth.sqlite3`，可用 `HEALTH_AGENT_AUTH_DB` 指定本地路径。开户命令与服务必须使用相同配置。
- SQLite 文件仅供单机基础验证；PostgreSQL 路径需在目标环境验收。健康记忆仍遵循原机制：没有 `DATABASE_URL` 时只在内存中，服务重启会丢失；账号持久化不等于健康记录持久化。
- `HEALTH_AGENT_AUTH_MODE=required` 要求登录；真实模型模式默认启用。`demo` 仅允许搭配 `HEALTH_AGENT_MOCK=1`，Mock 默认免登录，以保留离线演示和原验收行为。

本地无模型、带登录的启动方式：

```bash
HEALTH_AGENT_MOCK=1 HEALTH_AGENT_AUTH_MODE=required HEALTH_AGENT_COOKIE_SECURE=0 uv run uvicorn app.main:app --port 8000
```

`HEALTH_AGENT_COOKIE_SECURE=0` 只用于本地 HTTP 调试，默认 Secure Cookie 要通过 HTTPS 使用。反向代理应正确传递原始协议并仅信任指定代理，写请求还会校验 Origin。线上配置与部署文件本轮没有修改。

### 认证与隔离边界

| 接口 | 行为 |
|---|---|
| `POST /api/v1/auth/login` | JSON 为 `username`、`password`，签发 12 小时 HttpOnly、SameSite=Strict 会话 Cookie；响应仅含用户信息和到期时间 |
| `GET /api/v1/auth/me` | 返回当前用户；登录模式未登录返回 401；演示模式返回 `required: false` |
| `POST /api/v1/auth/logout` | 撤销当前服务端会话并清除 Cookie |

上述接口也存在 `/api/auth/...` 兼容路径。写请求需携带 `X-HealthPilot-Request: 1`，普通跨站表单不能满足；没有开放跨域凭证访问。自定义客户端应保存服务端 Cookie，禁止把 Cookie 或密码放 URL。

聊天、历史、随访、健康工具均在登录模式下鉴权。历史和随访可省略 `user_id`；如果提供，必须与当前账号 ID 一致。服务端使用自身身份确定数据归属，`thread_id` 首次使用时原子绑定账号，其他账号不能续跑。账号与演示使用不同数据命名空间，既有匿名数据不会自动绑定账号；引入命名空间前的演示记忆也不会自动迁移。

前端登录后不再使用浏览器自造的用户 ID；退出、到期及跨标签登录变更时卸载健康页面。请求携带当前页面预期账号，服务端拒绝因 Cookie 切换而产生的账号错配。退出后拒绝新请求，并在流式输出的下一事件边界复验会话；已经进入执行中的节点不承诺事务回滚，直接 `/chat` 的执行生命周期仍与请求绑定；新任务接口的取消机制见下文。

知识库继续使用独立 `ADMIN_PASSWORD`，普通用户登录不会得到后台权限。登录模式下未配置管理员密码则关闭后台数据接口；Mock 免登录模式沿用原有后台行为。当前知识库是共享知识库，尚无组织私有文档权限。

### 错误和基础审计

所有响应包含服务端生成的 `X-Request-ID`；HTTP 错误保留原 `detail`，增加 `code`、`request_id`、`retryable`、`next_action`。聊天执行错误在 SSE `error` 事件中增加这些诊断字段，`done` 事件保持兼容。`retryable` 不代表写操作可无条件重放。新 `/tasks` 已提供幂等提交；原 `/chat`、知识库上传及发布尚无业务幂等。

登录模式记录开户、密码重置、登录、退出、API 请求及结果、聊天完成或中断。记录包含操作主体、时间、资源、动作、状态与请求编号，不保存正文、查询参数、密码和 Cookie；账号模式的 Agent 文件轨迹只保留节点元数据和请求编号。管理员口令为共享凭证，审计主体是 `knowledge-admin`，不能据此识别具体操作人员。审计失败阻断新业务请求；已执行动作的审计故障须人工核对。当前是可关联的数据库审计，未实现防篡改、独立审计角色或分布式 span。

本地验收命令：

```bash
uv run pytest -q
uv run pytest -q verification/test_foundation.py
uv run python -m eval.run_eval --mock
cd frontend
npm test
npm run build
```

新增前端验收位于 `frontend/verification/`，不修改原有受保护测试。原有用例覆盖演示模式，新用例显式开启登录模式，覆盖越权、会话撤销、限流、CSRF、审计及页面清理。

## 第二阶段 任务持久化与幂等

登录后的聊天现在通过持久化任务提交；免登录演示和原 `/api/chat` 保留原有直接 SSE 行为。任务存入与账号相同的数据库，事件与任务分表保存。关闭页面、断开订阅或退出登录不撤回已经授权提交的任务；重新登录后可在“任务记录”查看结果，或主动请求取消。

| 接口 | 行为 |
|---|---|
| `POST /api/v1/tasks` | 提交 `{thread_id, message, web_search?}`，必须带 `Idempotency-Key` 与本站写请求校验头；首次 202，重复 200，异参复用键 409 |
| `GET /api/v1/tasks` | 查看本人最近任务，默认 30 条，`limit` 最大 100 |
| `GET /api/v1/tasks/{id}` | 查询状态、原始请求编号、取消标志、错误码及 `health_committed` 写入凭据 |
| `GET /api/v1/tasks/{id}/events?after=序号` | SSE 续读；事件包含递增 `seq`，结束事件也保存；不会再次执行任务 |
| `GET /api/v1/tasks/{id}/event-page?after=序号` | JSON 分页读取已保存事件，每次最多 200 条 |
| `POST /api/v1/tasks/{id}/cancel` | 排队任务直接取消；运行任务协作停止；已完成任务保持原状态 |

所有接口也提供 `/api/tasks/...` 别名，执行相同账号校验。幂等键范围是当前账号；同键同参返回原任务，包括失败或中断的原任务，不会隐式重跑。前端提交响应丢失时复用同键重试，事件订阅短暂中断时按序号续读并去重。

状态流转：`pending → running → succeeded / waiting_human / failed / cancelled / timed_out / interrupted`；排队阶段也可直接进入 `cancelled`。`waiting_human` 表示该轮返回了随访追问，用户回答作为新任务提交到同一 `thread_id`。这是逐轮结果，不是审批流；旧轮状态不会因后续回答而改写。任务列表可恢复选定任务的结果和引用展示，并继续所属会话；恢复展示不会改写服务端健康记录，继续执行使用持久化会话上下文。

默认由 API 进程内的 2 个消费者处理数据库队列，与 HTTP 订阅分离；同一账号仅一个任务占用执行槽。配置 PostgreSQL 时，健康图还使用账号级数据库 advisory lock，使任务执行与旧 `/chat` 在多个进程间串行化；单机无数据库模式仅支持一个 API 进程，不能用多个独立内存 Store 提供一致的健康记录。

独立 Worker 模式需提前配置同一 `DATABASE_URL`，以及相同的模型/Mock 配置：

```bash
# API 只提交与查询任务
HEALTH_AGENT_AUTH_MODE=required HEALTH_AGENT_TASK_WORKER=external uv run uvicorn app.main:app --port 8000
# 单独的终端或进程消费数据库队列
uv run python -m app.worker
```

`HEALTH_AGENT_TASK_TIMEOUT` 默认 300 秒，允许 1–3600 秒。执行租约为 30 秒，消费者周期性续租和检查取消。关闭 API 不会停止独立 Worker；默认内嵌模式的进程关闭会标记运行任务中断。

恢复边界：未开始的 `pending` 任务重启后可重新领取；已保存事件可从序号续读。运行进程崩溃后，租约过期会标记 `interrupted` 并保留已有事件。PostgreSQL 中，健康档案、历史、随访计划及任务写入凭据在同一个事务内保存；写入前锁定任务并校验账号、Worker token、租约与取消状态。同任务同类写入只提交一次，参数变化拒绝覆盖。图检查点和任务终态仍单独提交，因此结果不明的任务不会自动重放。取消、超时也不回滚已经写入的记录。没有 PostgreSQL 时健康记忆仍在内存，重启后保存的任务结果可读，但不能保证恢复健康图上下文。

当前任务输入和事件含健康信息，存储于受账号权限保护的数据库，不进入普通审计正文；尚未提供任务数据加密、自动过期清理或组织权限。没有新增 Redis、外部消息中间件、自动扩缩和配额功能。

`health_committed` 在 PostgreSQL 下表示该任务是否已原子提交健康记录：`true` 时即使任务中断也应先核对历史，不能把新幂等键当作原任务重试；`false` 表示尚无该任务的健康写入凭据（运行中可能变化）。SQLite + 内存健康记忆返回 `null`，不声称具备同等持久化保障。评估历史带 `task_id`，可与任务及写入凭据核对。同键重试只查询原结果；新键是独立业务请求，不能保证对人工重复提交去重。

多个 API/Worker 同时首次启动时，账号、任务、LangGraph 和知识库初始化使用同一数据库级锁协调，避免迁移表并发创建冲突。该锁不等同于完整的数据库版本升级、回滚体系。

新增验收：`verification/test_tasks.py` 覆盖任务接口与 SQLite；`frontend/verification/tasks.test.tsx` 覆盖同键重试、事件游标、任务查看及会话恢复。`verification/test_postgres.py` 使用真实 PostgreSQL 16 + pgvector，覆盖多 Worker 与旧接口并发、API/Worker 重启、随访检查点续接、提交前后进程崩溃、事务回滚、写入去重、取消/超时及并发首次初始化。故障注入仅存在于 `verification/pg_worker_probe.py`，不会进入正常 Worker 启动流程。

复现 PostgreSQL 验收：启动包含 pgvector 的隔离测试实例，将 `HEALTH_AGENT_TEST_POSTGRES` 设置为有创建数据库权限的测试连接串，然后执行：

```bash
.venv/bin/python -m pytest verification/test_postgres.py -q
```

没有设置该变量时，这组集成测试会明确跳过。设置后每例创建唯一临时数据库、启动独立测试进程，并在结束时清理数据库与进程；不会读取 `.env` 中的生产数据库地址作为测试目标。只在测试数据库上运行。本轮本机集成验收不代表生产部署、容量、高可用或备份恢复已验收。

## 第三阶段 知识变更确认与失败处理

正式登录模式（`HEALTH_AGENT_AUTH_MODE=required`）下，发布知识和删除整份文档必须经过服务端确认。直接调用原 `/api/v1/admin/documents/{id}/publish` 或文档 DELETE 返回 `428 APPROVAL_REQUIRED`，两套 V1 路径一致；旧管理员客户端需要接入下述流程。Mock 免登录演示保留原操作方式，以兼容既有演示，不能将该模式用于真实用户服务。

1. `POST /api/v1/admin/approvals`，提交 `{document_id, action: "publish" | "delete"}`，返回 `waiting_human` 确认单。摘要包含文档标题、版本、片段数、风险说明及发布时会被删除的旧文档。
2. 人工核对后，`POST /api/v1/admin/approvals/{id}/decision`，提交 `{decision: "approve" | "reject", fingerprint}`。批准后同一请求自动继续执行；拒绝不改知识库。内容指纹必须使用服务端返回值。
3. `GET /api/v1/admin/approvals` 查看当前管理员凭证下最近 50 条记录；`GET /api/v1/admin/approvals/{id}` 查询具体结果。重复批准已成功记录返回保存结果，执行中的重复请求返回 409，不会重复执行。

这些接口沿用管理员 Bearer 鉴权，不向普通账号开放。确认单绑定当时的管理员凭证；口令变更后旧确认单不能继续使用，执行前再次校验当前权限。当前管理员仍为共享口令，审计主体是 `knowledge-admin`，不能识别具体自然人；这是一人单级确认，不是多人复核、角色审批或完整企业认证。

确认有效期为 5 分钟，服务端拒绝任何过期执行，查询或决定时将过期状态落库；没有新增定时服务。执行阶段最多 120 秒。确认状态包含 `waiting_human / executing / succeeded / rejected / expired / invalidated / failed / uncertain`，与聊天任务的随访问答状态分开。内容、旧版本集合或权限变化会拒绝执行；进程退出后遗留的 `executing` 在有效期后标记 `uncertain`，须先核对结果，不自动重新执行。

PostgreSQL 中，内容指纹校验在知识库写锁内进行，文档变更、审批成功状态与执行审计同事务提交；任何一步失败会回滚，包括同标题旧版本删除。确认单保留操作摘要及成功结果，需与其他知识数据一并考虑权限、留存和备份。SQLite + 内存知识库仅用于本地演示：进程内取消保持确认结果和内存一致，但进程崩溃或重启后不保证知识库持久化。

后台新增“确认记录”标签页。发布遇到确认要求时显示核对窗口；删除仍保留原先的浏览器提醒，再展示服务端确认单。关闭窗口不会批准操作，可在有效期内从确认记录重新打开。

失败任务处理：

- `GET /api/v1/tasks?failed=true` 仅列出本人失败、取消、超时或中断任务；返回 `failure_category`、`health_committed` 与 `review_disposition`。
- `POST /api/v1/tasks/{id}/review` 提交 `{disposition: "resolved" | "needs_support"}`，记录“已核对”或“需要人工协助”；重复相同决定不重复写审计。
- 前端可过滤失败任务，并提示已保存或无法确认的健康记录。处理记录不会修改任务终态，也不会重新执行任务。`needs_support` 只是待处理标记，尚未发送通知或创建客服工单。

B3 基础范围不包含多级审批、移动端确认、人工服务通知、自动失败重试或任意节点恢复。新增验收位于 `verification/test_approvals.py`、`verification/test_approvals_postgres.py` 和 `frontend/verification/approvals.test.tsx`。PostgreSQL 集成仍通过显式 `HEALTH_AGENT_TEST_POSTGRES` 使用临时数据库。

## 无需搜索密钥的联网功能

聊天输入框上方可开启“联网搜索 / 读取链接”，默认关闭。开启后无需先填写风险评估档案，急症规则仍优先执行；正在进行的随访问诊需先完成或取消，再使用联网问答。

- 输入健康主题：通过公开 Bing RSS 搜索获取链接，过滤明显无关结果；中文结果不足时，按常见健康主题补充英文关键词重试，最后尝试 DuckDuckGo 公开 HTML 搜索。不需要 Google、Tavily 等搜索 API Key。
- 粘贴公开 HTTP(S) 链接：跳过搜索，直接读取最多 3 个网页或 PDF。可在轨迹里查看搜索结果、读取失败原因、已读取来源、获取时间和原文链接。
- 搜索和读取均通过 MCP 子进程，网页不进入健康计算的长期缓存。网络资料仅供本轮检索使用，不自动发布到知识库；会话检查点仍按原有方式保存状态。轨迹文件省略网络问题、网址和原文。
- Mock 模式默认离线；显式开启联网后会发起网页请求，但只展示来源。真实模式仍使用已有千问配置生成回答；未配置模型时也只展示来源，无需申请搜索密钥。生成回答的模型费用另计。
- 搜索会发送本轮输入的问题，不发送历史对话或健康档案。请使用主题关键词，不输入个人身份信息。网络来源均标记为未经医学审核；现有引用校验不等于医学事实校验。

公开搜索页没有稳定性保证，可能出现限流、验证码、结果不相关或页面结构变化。只读取无需登录的静态 HTML、纯文本和文字版 PDF，不绕过验证、付费墙，也不运行网页 JavaScript。每份文件最多 8 MB，PDF 最多 80 页，最多提取 60,000 字符，再选择相关片段；不保证读完全文。搜索或正文不足会明确提示，可直接提供原文链接重试。仅允许公网标准端口，每次跳转重新校验地址，并固定已校验的连接 IP。

2026-11-03 新增功能验证：原有后端 219 项、前端 108 项测试通过；另用临时脚本验证地址拦截、正文/PDF 解析、引用拒答、急症优先和开关隔离等 30 项检查。通过真实 MCP 搜索“高血压 运动 指南”，补充英文主题后找到 WHO 高血压资料并成功提取正文；同轮其他网站出现拒绝访问，已显式返回失败。此检查仅验证技术链路，未开展联网回答的医学质量评测。

## 测试和评测

```bash
uv run pytest -q
uv run python -m eval.run_eval --mock
cd frontend && npm ci && npm test
```

2026-10-30 用 `qwen3.6-plus` 跑了一次真实模型评测：26 条用例全部通过，每轮响应 p50 约 3.7 秒、p95 约 5.2 秒。

这个结果的参考价值有限：用例是开发时自己写的，数量很少；"医学事实性"一项只检查引用是否有效，不检查内容是否正确。

### 检索选型

`eval/run_rag_eval.py` 用 50 道题对比三种检索（语料 16 段，题目是自己写的）：

| 方案 | 首条命中 | 前 3 条命中 |
|---|---|---|
| BM25 | 76.3% | 92.1% |
| BM25 + 向量（RRF 融合） | 84.2% | 94.7% |
| 向量 | 100% | 100% |

所以选了纯向量。语料小、主题差别大，这个 100% 不代表真实效果。

相关性门槛（余弦相似度 0.44）在这批题上调出来，再用没参与调参的 35 道新题（`eval/rag_holdout.jsonl`）验证：21 道可答题中 19 道首条正确且未被误拦；14 道语料答不了的题挡住 10 道。漏过的 4 道都是话题相近但人群不同（如孕妇、透析），要靠提示词让模型说明资料未覆盖，抽查的几道都做到了，但没有做系统统计。

## 已知限制

- **风险模型**：Framingham 基于美国人群，可能高估中国人群风险，国内更适合用 China-PAR。这里还没有实现
- **语料**：内置的只有 16 段摘要，按公开指南要点撰写，没有逐条对照原文，也没有文献和药品说明书
- **幻觉**：校验只查引用是否有效，不查内容是否被资料支持。抽查发现模型拒答时可能补充资料之外的知识
- **PDF 解析**：不支持扫描版；表格解析没有专门处理，发布前要人工核对
- **急症识别**：只靠关键词规则，换种说法可能识别不出来
- **随访**：提醒只在用户打开页面时出现，没有短信或微信推送，也没有医生审核；用药和症状的判断是简单的关键词规则
- **部署**：运行在一台 1G 内存的海外轻量服务器上，用的是临时域名（sslip.io），从中国内地访问的速度和稳定性没有系统测过
- **规模**：只做过单人使用，没有压测。不配置 `DATABASE_URL` 时记忆存在内存里，重启就丢
- **安全**：本地实现了账号、可撤销会话、用户隔离及基础审计，尚无组织、SSO、多角色、完整加密治理或防篡改审计。Mock 免登录模式没有个人数据访问保护，仅用于虚拟数据。代码未部署到在线演示，不能据此判断远端已经启用登录。
- **许可证**：PyMuPDF 是 AGPL，闭源商用需要换库或购买授权
