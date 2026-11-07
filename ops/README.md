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
- `/api/v1/admin/guest`：PATCH `{"paused": true}` 暂停免登录体验，首页改为只显示登录页，已开启的游客不能再发送消息；`false` 恢复。运维后台“运行状态”有同样的按钮。

队列容量通过数据库同一行锁检查，覆盖多进程同时提交，不仅靠前端按钮。每账号执行槽仍串行，优先级只改变可领取任务间的顺序。当前没有自动扩缩容或低优先级等待上限保证。

| 配置 | 默认值 | 含义 |
|---|---|---|
| `HEALTH_AGENT_TASK_WORKERS` | 2 | 每个 Worker 进程消费者数，1–32 |
| `HEALTH_AGENT_QUEUE_LIMIT` | 200 | 全局 pending + running 上限 |
| `HEALTH_AGENT_USER_QUEUE_LIMIT` | 8 | 单账号 pending + running 上限 |
| `HEALTH_AGENT_QUEUE_ALERT_SECONDS` | 60 | 管理页排队等待告警阈值 |
| `HEALTH_AGENT_RELEASE` | local | 展示发布版本；不得放入凭证 |
| `HEALTH_AGENT_GUEST` | 1 | 登录模式下是否开放免登录体验；`0` 关闭，首页只显示登录页 |
| `HEALTH_AGENT_GUEST_MESSAGES` | 20 | 每位游客可发送的消息数（整个体验期内） |
| `HEALTH_AGENT_GUEST_DAILY_MESSAGES` | 200 | 全站游客每天（北京时间）可发送的消息总数；`0` 等同关闭 |
| `HEALTH_AGENT_GUEST_PER_IP` | 5 | 同一网络 24 小时内可开启的游客数 |
| `HEALTH_AGENT_GUEST_HOURS` | 24 | 游客会话时长；到期后约 1 小时内删除其全部数据 |
| `HEALTH_AGENT_TRUSTED_PROXIES` | 回环与私有网段 | 只信任这些地址传来的 `X-Forwarded-For`，用于按网络限流；直接暴露公网时不受伪造头影响 |

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

## OpenTelemetry 日志与跨进程链路

日志和链路已迁移到 structlog + OpenTelemetry SDK → Collector → Loki/Tempo → Grafana。原自研 SQLite 补传线程与 PostgreSQL 日志写入已停用；旧数据不删除。HTTP、队列、Worker、Agent、模型、RAG、MCP 和数据库调用通过标准追踪上下文关联。配置、部署、历史迁移和验收边界见 [OpenTelemetry 运维说明](OBSERVABILITY.md)。

旧版 `HEALTH_AGENT_LOG_DATABASE_URL`、`HEALTH_AGENT_LOG_SPOOL`、`HEALTH_AGENT_LOG_BUFFER_ROWS`、`HEALTH_AGENT_LOG_RETENTION_DAYS` 不再控制新链路。部署时增加 `ops/compose.observability.yml`，设置新镜像、Grafana 密码和遥测 HMAC 密钥。新保留期由 Loki/Tempo 配置管理。

## CI 触发与等待时间

工作流先按整次 push/PR 的变更判断范围。只有 README、验收矩阵、`docs/`（含截图）或运维说明变化时，仅执行轻量分类，跳过测试和镜像发布。一个 push 同时包含之前积累的代码提交时仍执行完整检查，不以最后一条提交标题判定。手动运行工作流始终执行完整流程。

后端检查改为失败即停、逐条显示用例名；超过 60 秒的用例打印等待堆栈，整步五分钟后终止，必要时 15 秒后强制结束。检查作业总上限十五分钟，前端与依赖步骤分别限时；同一分支的新代码检查取消旧检查。镜像仅在检查成功后发布，不将超时或失败伪装成成功。

纯文档运行成功不代表该 SHA 有镜像。自动部署依赖 `publish-image` 作业成功，部署本次工作流的完整 SHA；纯文档推送不发布镜像、不触发部署。旧的本地 `healthpilot-deploy` 仍按远端 HEAD 选版本，不建议与自动部署并发使用。

## GitHub Actions 自动部署

`main` 的 push 经检查和镜像发布后执行 `deploy` 作业。PR、其他分支、手动验收和跳过镜像的运行不会自动部署。GitHub 并发组与服务器文件锁避免同时更新容器，服务器拒绝历史分叉和非 main 提交，跳过已经被新版本取代的部署。

仓库 Actions 配置：

| 类型 | 名称 | 用途 |
|---|---|---|
| Secret | `HEALTHPILOT_DEPLOY_SSH_KEY` | 独立的 Ed25519 部署私钥 |
| Secret | `HEALTHPILOT_DEPLOY_KNOWN_HOSTS` | 从可信连接取得的服务器主机公钥，不在每次构建时盲目信任扫描结果 |
| Variable | `HEALTHPILOT_DEPLOY_HOST` | `用户@主机`，SSH 端口 22 |
| Variable | `HEALTHPILOT_DEPLOY_ENABLED` | 服务器安装与检查成功后设为 `true`；设为 `false` 可暂停后续自动部署 |

服务器初始化：将 `ops/ci-deploy.sh`、`ops/install-ci-deploy.sh` 和部署公钥送到服务器同一临时目录，用现有管理员连接运行 `bash install-ci-deploy.sh 公钥文件`。安装器保留已有授权，将入口复制到 `/usr/local/libexec/healthpilot-ci-deploy`，专用公钥只允许 `check` 与 `deploy 完整SHA`，禁止交互 shell、PTY 和转发。私钥只交给 Actions，应用 `.env` 继续保留在服务器。服务器需能无交互读取 GitHub 仓库及公开 GHCR 镜像，部署目录默认为 `/root/health-agent`。

先用新密钥连接并执行 `check`，通过后再启用开关。部署入口先拉取固定 SHA 镜像，再将服务器代码快进到相同提交，最后调用 `ops/release.sh` 更新 API 和 Worker。保留服务器自定义 Caddyfile；目标提交同时修改该文件时停止。现有代码回滚不回滚数据库。

入口更新需通过管理员连接重新运行安装器，专用密钥不能替换自身授权配置。密钥轮换时先安装新公钥，替换 GitHub Secret 并验证，再删除旧公钥。停用自动部署时关闭开关；撤销凭证还需从服务器 `authorized_keys` 移除对应公钥。
