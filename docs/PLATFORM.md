# 平台能力与接口说明

从 [README](../README.md) 移出的运行配置、HTTP 接口和各阶段实现说明，内容保持原样。完整目标与验收进度见 [验收矩阵](../ACCEPTANCE_MATRIX.md)。

## 目录

- [运行](#运行)
- [API V1](#api-v1)
- [本地验收与第一批基础建设](#本地验收与第一批基础建设)
  - [创建账号与启动](#创建账号与启动)
  - [认证与隔离边界](#认证与隔离边界)
  - [错误和基础审计](#错误和基础审计)
- [第二阶段 任务持久化与幂等](#第二阶段-任务持久化与幂等)
- [第三阶段 知识变更确认与失败处理](#第三阶段-知识变更确认与失败处理)
- [无需搜索密钥的联网功能](#无需搜索密钥的联网功能)
- [第四阶段 知识权限、撤回与模型治理](#第四阶段-知识权限撤回与模型治理)
  - [B4 补充：版本档案、文本导入与定价](#b4-补充版本档案文本导入与定价)
- [第五阶段 多会话工作台](#第五阶段-多会话工作台)
- [第六阶段 运维与发布基础](#第六阶段-运维与发布基础)
- [集中日志与完整应用链路](#集中日志与完整应用链路)

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

用 Docker 部署（含 Postgres、HTTPS 和访问口令）见 [docs/DEPLOY.md](DEPLOY.md)。

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

完整的 51 项目标、依赖、验收条件及进度见 [验收矩阵](../ACCEPTANCE_MATRIX.md)。第一批实现用户登录、数据隔离、请求编号、标准错误及基础审计；尚未达到企业级。

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

知识库继续使用独立 `ADMIN_PASSWORD`，普通用户登录不会得到后台权限。登录模式下未配置管理员密码则关闭后台数据接口；Mock 免登录模式沿用原有后台行为。B4 增加共享与指定账号的文档范围，仍无组织层级权限，详见第四阶段。

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

## 第四阶段 知识权限、撤回与模型治理

正式登录模式下，知识文档支持 `shared`（共享）和 `restricted`（指定账号）两种访问范围。默认共享；指定账号最多 100 个，空列表表示仅知识库管理员可检索。管理员可在文档详情点击“访问范围”生成确认单，核对账号标识后执行。授权使用服务端账号 ID，不能通过聊天参数、提示文本或请求头伪造。账号选择接口当前最多返回 500 个账号；组织、部门、角色继承和 SSO 尚未引入。

发布、访问范围调整和撤回均沿用 B3 的内容指纹、期限、凭证复验与执行审计。新版本草稿默认共享，发布确认单会展示该草稿的实际范围；上传受限资料时应先调整草稿的访问范围，再发布。撤回不删除原文；管理员仍可查看详情，但问答检索和后台检索测试均不再召回该文档。撤回暂不支持直接恢复，可上传新草稿重新审核发布。已有“同标题、同类型发布会删除旧文档”的规格仍保留，因此尚不支持永久版本档案。

| 接口 | 行为 |
|---|---|
| `GET /api/v1/admin/accounts` | 返回可选择账号的 ID 与用户名；需要管理员凭证 |
| `POST /api/v1/admin/approvals` | `action: "access"` 携带 `access: {visibility: "restricted", allowed_user_ids: [账号ID]}`；生成待确认单 |
| `POST /api/v1/admin/approvals` | `action: "withdraw"` 与 `document_id`；生成撤回确认单 |
| `POST /api/v1/admin/approvals/{id}/decision` | 与 B3 一样，携带决定及内容指纹后执行 |
| `GET /api/v1/admin/model-usage` | 返回最近七天按模型、场景及状态聚合的调用尝试、已知 Token、缺失用量次数和耗时；金额为 `null` |

上述接口都有 `/api/admin/...` 兼容路径。普通账号无法访问管理接口。PostgreSQL 中权限/撤回、确认成功及审计在同一事务提交。内存知识库仍不提供跨进程持久化，正式试点应使用 PostgreSQL。

向量检索与 BM25 在排序和返回结果前过滤权限；查询向量缓存不保存检索结果。引用增加文档 ID、版本及修订信息。任务事件回放重新校验资料可见性与指纹；权限改变、撤回或删除后隐藏失效来源卡片及相关回答。新回答记录实际读取过的全部依据，模型漏写某个引用也不能使对应历史回答绕过复验。历史上下文送入模型、备用模型调用前及最终输出前均再次检查，已经发给模型或渲染到浏览器的内容无法召回。旧记录缺少完整依据指纹时只能依赖已有引用校验；本批不宣称解决旧记录所有语义依赖或浏览器离线副本。

真实模型统一通过 `app/model_gateway.py`。主模型仍由 `QWEN_MODEL` 指定；可选 `QWEN_FALLBACK_MODEL` 使用相同供应商地址和凭证。主模型失败时最多尝试一次备用模型；SDK 自动重试关闭。所有模型共享同一轮预算、当前文档权限和确定性输出校验，故障后沿用原有模板或拒答，不替代医疗护栏。

| 配置 | 默认值 | 范围与含义 |
|---|---|---|
| `HEALTH_AGENT_LLM_MAX_CALLS` | `12` | 每轮最多 1–100 次调用尝试，包含工具循环、校验重答、排队失败及备用模型 |
| `HEALTH_AGENT_LLM_MAX_INPUT_CHARS` | `60000` | 单次消息内容字符数上限，允许 1000–500000；不是 Token 或金额限额 |
| `HEALTH_AGENT_LLM_CONCURRENCY` | `4` | 每个 Agent 实例同时最多 1–32 次模型调用；工具绑定实例共享限制 |
| `QWEN_FALLBACK_MODEL` | 未设置 | 可选备用模型；仍可能产生供应商费用 |

并发排队最多 2 秒，供应商调用上限 35 秒（SDK 请求超时 30 秒）。同一模型连续三次上游失败后暂时熔断 30 秒；排队繁忙不计上游失败。以上是进程内保护，尚无跨 Worker 总配额、优先级、按金额预算或自适应限流。

正式账号调用记录写入 `hp_model_usage`，关联用户、任务（直接聊天可为空）、请求、模型及提示场景版本，只保存用量与状态，不保存提示正文、密钥或异常正文。仅统计提供商返回的 Token，缺失不当作零；后台显示缺失次数。记录在本轮退出时批量写入，进程强制退出可能丢失尚未落库的用量，因此不是计费账本。模拟模型、向量模型、提供商价格表和账单核对不在本批统计范围。

新增验证：`verification/test_governance.py`、`verification/test_governance_postgres.py`、`frontend/verification/governance.test.tsx`。检索质量提升（混合召回、重排）、持续评测门禁、完整版本归档、实名多级审批、分布式模型配额和费用预算仍需后续建设。

### B4 补充：版本档案、文本导入与定价

后台新增“版本档案”“文本导入”。经确认的发布、撤回和权限调整会将变更前后原文归档；同标题旧文档仍按规格从生效库删除，但档案保留供管理员查询。显式删除文档同时清除该文档档案。档案不参与问答，不向普通用户开放，也不会追溯恢复升级前已删除的内容。纯文本/Markdown 通过 `/api/v1/admin/text-documents` 导入为草稿，仍需核对并确认发布。

可选 `QWEN_MODEL_ROUTES` JSON 将 `extract`、`answer`、`advise`、`intent` 映射到同供应商模型名。各场景共用单轮预算和并发限制。可选 `HEALTH_AGENT_MODEL_PRICES` JSON 按模型配置 `input_per_million`、`output_per_million`（每百万 Token 的美元价格），`HEALTH_AGENT_PRICING_VERSION` 标记价格版本。只有 Token 和单价都齐全才记录微美元估算；历史记录不随价格变更重算，金额仅供统计，不能代替实际账单。按 `docs/ADMIN.md` 保留向量优先策略，不引入已被规格排除的混合检索。

本轮按用户要求仅执行 Python 编译与前端构建，功能验收由用户进行。

## 第五阶段 多会话工作台

登录后的“我的会话”支持标题搜索、分页、重命名、归档/恢复、打开多轮历史和导出 Markdown。首次提交任务自动创建会话，升级时从已有任务补建会话索引。归档仅停止该会话的新任务提交，不删除历史，不取消正在运行的任务；重复幂等请求仍返回原任务。打开历史按 20 轮分页，继续提问始终使用服务端最新上下文，不会倒退服务端状态。

- `GET /api/v1/conversations?archived=false&q=&offset=0`：本人会话，每页 30 条。
- `PATCH /api/v1/conversations/{id}`：更新 `title` 或 `archived`。
- `GET /api/v1/conversations/{id}/turns?before=游标&limit=20`：查询历史轮次，重新校验资料权限。
- `GET /api/v1/conversations/{id}/export`：下载最近 50 轮 Markdown 和可见引用，超过 200 个事件的轮次提示去任务记录读取全文。

所有入口提供 `/api/...` V1 兼容路径；每次按登录账号检查归属，导出不创建公开链接。导出文件包含个人对话，由下载者自行保管。原 `/chat` 演示路径不写入新工作台；正式登录前端使用 `/tasks`。

对话新增“停止当前任务”、可折叠执行步骤与节点耗时；来源卡片可直接展开原文并定位高亮。不展示未经可靠校准的剩余时长。管理员与用户工作台继续使用独立鉴权；实名多级审批、组织角色与 SSO 需要组织模型及身份提供商接入，不能仅凭这些界面宣称已完成。

## 第六阶段 运维与发布基础

后台新增“运行状态”：Worker 心跳、队列长度和等待时间、任务失败分类、人工协助标记、已知费用估算、按请求编号查询审计；管理员可暂停/恢复新任务接收并调整待执行任务优先级。全局和单账号队列容量在数据库锁内校验；超额返回 429，暂停返回 503，旧幂等请求仍正常回放。

新增 `/api/v1/ready` 就绪检查、管理员鉴权的 Prometheus 指标、JSON 响应头日志及运行配置指纹。新增加密备份/隔离恢复 CLI、监控和计划备份示例、独立 Worker 部署覆盖文件与固定镜像发布回滚脚本。所有使用方法与配置见 [运维说明](../ops/README.md)。原 Dockerfile、Compose 和 Caddyfile 未改动。

本轮只完成开发、Python 语法编译、Shell 语法和 TypeScript/Vite 构建，不新增或运行测试，不进行浏览器验收。没有部署、执行备份恢复、发送告警或调用真实模型。功能验收交由用户；组织 SSO、实名多级审批、分布式模型费用硬预算、多地域恢复和容量/安全演练仍需组织信息与实际基础设施，不能宣称企业级验收全部通过。

## 集中日志与完整应用链路

新增后台“日志检索”：按时间、级别、服务、请求/任务/链路 ID 和关键词筛选，查看父子 span、步骤状态和耗时，导出当前页或已加载链路。API、独立 Worker、Agent 节点、模型调用、知识检索和 MCP 子进程通过持久化上下文关联；所有服务统一 JSON 元数据日志，正文和密钥不写入日志。

日志默认进入现有 PostgreSQL，可通过 `HEALTH_AGENT_LOG_DATABASE_URL` 指向独立日志库；无数据库时使用本地 SQLite 开发模式。包含持久缓冲、失败重试、事件去重、保留期清理、缓冲指标和告警规则。重启 API/Worker 后生效；部署与接口详见 [运维说明](../ops/README.md#统一日志与跨进程链路)。本轮按要求不运行测试，编译与构建不能替代生产部署验收。
