# HealthPilot 运维入口

这些工具已实现但本轮未连接生产环境执行。上线前由运维负责人确定容量、RPO/RTO、保留周期、值守接收方和回滚窗口；本仓库没有自动创建云资源或生产账号。

## 状态与调度

- `/api/v1/health`：现有存活和模式检查。
- `/api/v1/ready`：检查 Agent、MCP 和有效 Worker 心跳，未就绪返回 503，不暴露连接地址。
- `/api/v1/admin/operations`：后台“运行状态”的数据源，含任务计数、有效 Worker、排队时长、24 小时失败分类和已知费用估算。
- `/api/v1/admin/metrics`：Prometheus 文本指标，使用管理员 Bearer 鉴权；不包含健康正文、用户名或请求正文。
- `/api/v1/admin/audit?request_id=...`：关联请求审计；最近记录按 100 条分页。
- `/api/v1/admin/admission`：PATCH `{"paused": true}` 暂停新提交，`false` 恢复；既有任务继续，既有幂等键仍返回原结果。
- `/api/v1/admin/queue/{task_id}/priority`：PATCH `{"priority": 0}`，范围 0–9，只能改 pending，管理端审计留痕。

队列容量通过数据库同一行锁检查，覆盖多进程同时提交，不仅靠前端按钮。每账号执行槽仍串行，优先级只改变可领取任务间的顺序。当前没有自动扩缩容或低优先级等待上限保证。

| 配置 | 默认值 | 含义 |
|---|---|---|
| `HEALTH_AGENT_TASK_WORKERS` | 2 | 每个 Worker 进程消费者数，1–32 |
| `HEALTH_AGENT_QUEUE_LIMIT` | 200 | 全局 pending + running 上限 |
| `HEALTH_AGENT_USER_QUEUE_LIMIT` | 8 | 单账号 pending + running 上限 |
| `HEALTH_AGENT_QUEUE_ALERT_SECONDS` | 60 | 管理页排队等待告警阈值 |
| `HEALTH_AGENT_RELEASE` | local | 展示发布版本；不得放入凭证 |

Worker 每 5 秒报告心跳，20 秒内有效；一天前的失效心跳自动清理。队列运行参数在启动时校验并保存指纹快照 `hp_config_snapshots`。HTTP 响应头日志采用 JSON，含路由模板、状态、返回响应头耗时及 request_id；SSE 全流程耗时应查看任务完成指标，不记录正文或查询参数。日志集中收集、告警通知和长期归档由部署方接入。

使用 `prometheus.example.yml` 与 `alerts.yml` 接入已有监控；管理员凭证从受控文件读取，不能把实际密码提交到仓库。告警规则仅提供配置，尚未安装到任何监控系统。后台“需人工协助”是处理标记，不是客服通知。

## 加密备份与恢复

部署机需安装与 PostgreSQL 服务兼容的 `pg_dump`、`pg_restore` 和 GnuPG。数据库连接从 `DATABASE_URL` 读取，不放进命令行；备份口令从权限 600 的非空文件读取。口令不与备份存放在一起。

```bash
.venv/bin/python -m app.maintenance backup \
  --passphrase-file /run/secrets/backup-passphrase \
  --output /srv/backups/healthpilot-20261104.gpg

.venv/bin/python -m app.maintenance restore \
  --passphrase-file /run/secrets/backup-passphrase \
  --input /srv/backups/healthpilot-20261104.gpg \
  --target hp_restore_20261104
```

备份采用数据库一致性快照，同时记录核心表计数；数据库 dump、SHA-256 与清单一同加密。目标文件已存在时拒绝覆盖。临时明文仅放在当前用户可读的临时目录，进程正常退出时清理；被强制终止时可能遗留临时文件，应由主机受控清理策略处理。

恢复只能创建新的 `hp_restore_*` 数据库，存在则拒绝覆盖。先解密、校验摘要、恢复事务并比较核心表计数，再执行恢复隔离：撤销旧登录会话、清理 Worker/执行槽、将未完成任务标记为需核对、作废待确认审批、暂停新提交并留审计。随后不自动改应用连接，也不自动执行旧任务。只恢复本系统生成且来源可信的备份。

`backup.cron.example` 提供计划任务示例，未自动安装。异地复制、备份保留清理和定期恢复演练需要运维配置。当前未承诺任何 RPO/RTO。

## 发布与回滚

根部署文件保持不变，使用 `ops/compose.runtime.yml` 叠加 API/独立 Worker 的固定版本镜像。现有 CI 可以产出 `sha-提交号` 镜像。

```bash
bash ops/release.sh deploy ghcr.io/组织/镜像:sha-40位提交号
bash ops/release.sh rollback
```

发布脚本拉取指定镜像、更新 API 与 Worker、检查就绪；未就绪时请求恢复此前镜像。仅支持固定 SHA 标签或镜像摘要，不接受 latest。首次部署没有可回滚版本；已存在部署若使用非 GHCR 构建镜像，需先建立受控镜像基线。备份和必要的架构兼容审查应在部署前完成；代码回滚不回滚数据库内容。当前新增结构为兼容扩展，后续破坏性迁移必须单独制定回滚方案。

脚本不启动灰度流量切换，也未在生产运行。`ops/.state/` 保存本机发布状态，已排除 Git 和镜像构建。`runs/`、备份目录也排除镜像构建，避免把本地用户记录带入发布镜像。

## 故障处理

| 信号 | 首先处理 |
|---|---|
| `NO_WORKER` / ready 503 | 检查 Worker 是否启动、数据库连通性和启动配置；不通过重发历史任务解决 |
| `QUEUE_DELAY` / `QUEUE_CAPACITY` | 查看模型延迟和队列；必要时暂停新提交，按容量增加 Worker；不要重复创建幂等键 |
| `STALE_LEASE` / `WORKER_LOST` | 查看任务是否已有健康写入凭据，失去租约的任务需核对，不自动重放 |
| 模型超时、熔断 | 检查供应商状态和限额；备用模型仍遵守原权限与预算 |
| `RESTORE_REVIEW_REQUIRED` | 核对恢复数据与业务记录，人工决定后续请求；恢复环境默认停止新提交 |
| 审计不可用 | 先恢复数据库/审计写入，再恢复新请求；核对故障期间可能已完成的动作 |

## 统一日志与跨进程链路

API、Worker 和 MCP 统一输出 JSON 到 stderr，同时写本地持久缓冲，后台批量补传到集中日志库。字段包含事件 ID、毫秒时间、级别、服务/实例、trace_id、span_id、parent_span_id、request_id、task_id 和白名单元数据。健康正文、检索词、URL、工具参数/结果、提示词、Cookie、密码、密钥和异常正文不进入运行日志。普通第三方日志仅保留模板指纹、模块/位置、异常类型与不含源码的栈位置。操作审计仍由原有独立审计表负责，不将运行日志当作不可篡改审计。

HTTP（包括完整 SSE 生命周期）→ 任务提交 → 持久队列 → Worker → Agent → 节点 → 模型尝试/知识检索 → MCP 客户端 → MCP 子进程形成父子 span；任务上下文随入队事务持久化，独立 Worker 读取后恢复，取消、超时、降级和模型重试可定位。响应返回 `X-Request-ID`、`X-Trace-ID`。MCP 通过协议 `_meta.healthpilot` 传递 W3C traceparent，不更改健康工具入参。旧任务没有持久化上下文时由 Worker 新建链路；升级前日志无法补齐。模型服务端内部、数据库服务端内部不在本项目可追踪范围。

默认忽略客户端 traceparent，防止不可信调用者串入其他链路。仅在可信入口剥离客户端追踪头并重新生成后设置 `HEALTH_AGENT_TRUST_TRACEPARENT=1`。关联 ID 不是鉴权凭证。当前提供 W3C 格式传播和内置 span 存储，不声称已接入 OTLP、Jaeger 或其他外部追踪后端。

| 配置 | 默认值 / 用途 |
|---|---|
| `HEALTH_AGENT_LOG_DATABASE_URL` | 未设置时使用 `DATABASE_URL`；可指定独立 PostgreSQL 日志库，各 API/Worker 必须一致 |
| `HEALTH_AGENT_LOG_DB` | 无 PostgreSQL 时使用 `runs/logs/central.sqlite3`，仅供单机开发 |
| `HEALTH_AGENT_LOG_SPOOL` | `runs/logs/spool.sqlite3`；本地 SQLite WAL 持久缓冲，API/Worker 各自持久卷，MCP 与父进程共享 |
| `HEALTH_AGENT_LOG_BUFFER_ROWS` | 100000 条，允许 1000–1000000；达到上限按批丢弃最旧记录并累计丢弃数 |
| `HEALTH_AGENT_LOG_RETENTION_DAYS` | 30 天，允许 1–3650；后台分批清理过期日志，积压时清理可能延后 |

正式多实例部署需共同的 PostgreSQL 日志库；SQLite 文件不能跨主机承担集中检索。`ops/compose.runtime.yml` 已挂载日志持久卷并限制 Docker stderr 日志轮转，不要通过 `down -v` 删除待补传日志。独立日志库账号需要本库建表/索引及读写权限，连接凭证由环境/部署密钥提供，不写进仓库。日志库备份、TLS、磁盘容量与只读运维账户由部署方按环境配置；现有业务库备份不会自动覆盖独立日志库和待补传持久卷。

日志中心失败时重试间隔从 1 秒增加到 30 秒；每批 250 条，事件 ID 去重，中心提交后才删除本地记录。停机保留待补传数据。缓冲上限或磁盘故障可能丢日志，会输出脱敏故障事件并累计计数，不承诺无限缓存或绝不丢失。进程突然终止可能留下只有开始记录的 span，界面会明确标识。

后台 `/admin` → **日志检索**：按最近一小时/一天/七天/三十天、服务、级别、事件、请求/任务/链路 ID 和关键词查询，游标翻页、展开元数据、查看父子链路与耗时、导出当前页或已加载链路。PostgreSQL 关键词采用全文索引的词项匹配（不提供中文分词）；开发 SQLite 使用子串匹配。精确排障优先使用 ID/事件过滤。

接口均为 V1，继续受管理员鉴权、审计和 no-store 保护，正式登录模式才开放：

- `GET /api/v1/admin/logs`：`start/end` 毫秒时间，跨度最多 30 天；`limit` 最大 200；返回稳定的 `next_cursor`。
- `GET /api/v1/admin/traces/{trace_id}`：每页最多 1000 条，`before` 继续较早记录；未加载或过期的父步骤会显示提示。
- `GET /api/v1/admin/logs/status`：当前 API 实例缓冲、丢弃、最近补传和中心可用性，以及各服务最近入库时间。它不是所有 Worker 缓冲状态的汇总；独立 Worker 故障需同时检查其 stderr/容器状态及最近入库时间。
- `/api/v1/admin/metrics` 新增本 API 实例的 `healthpilot_log_buffer_pending`、`healthpilot_log_dropped_total` 和 `healthpilot_log_export_unavailable`；告警规则已加入示例文件，部署方仍需加载规则和配置通知接收方。

本轮仅做 Python 编译与前端构建，未运行功能/故障注入/负载测试，未部署日志中心或告警通知。生产容量、中心故障补传、跨主机时钟同步和恢复需实际验收；代码交付不等于企业级生产验收通过。
