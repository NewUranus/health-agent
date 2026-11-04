# OpenTelemetry 日志与链路

## 架构与实现

- `app/telemetry.py` 使用 OpenTelemetry SDK 的 TracerProvider、SpanProcessor、BatchSpanProcessor、LoggerProvider 和 BatchLogRecordProcessor；日志 JSON 序列化和业务上下文使用 structlog。OTLP 使用官方 HTTP/protobuf 导出器。
- 服务名为 `healthpilot-api`、`healthpilot-worker`、`healthpilot-mcp`。服务版本与实例标识作为 Resource，每条 JSON 日志带 `release`、request_id、task_id、thread_id、user_hash、trace_id、span_id。用户和会话标识使用带域区分的 HMAC，不保存原始 ID。
- HTTP/SSE 使用应用专用的 OTel 服务端 span；HTTPX 和 Psycopg 使用官方自动埋点。未再叠加 FastAPI 自动埋点，以免重复生成根 span、在鉴权前采集 URL 或遗漏流式响应尾部。LangGraph 节点、模型尝试、RAG、任务生产/消费与 MCP 是业务手动埋点，均为真实 OTel span。
- W3C TraceContext propagator 负责 inject/extract。任务上下文在业务入队事务内持久化；Worker 恢复上下文，MCP 用协议 `_meta.healthpilot` 传播。旧版本已经保存的 traceparent 可继续恢复。身份与业务权限始终由原来的认证和确定性代码负责。
- 导出前 SpanProcessor 只保留允许的元数据；自动埋点的 SQL、URL、事件、异常描述和 link 属性不会进入批处理队列。未安装采集提示词/模型回复的 LangChain/OpenInference 自动埋点。模型边界已有场景、模型、次数、Token、耗时和错误类别。
- Provider 显式注入受控的 HTTPX/Psycopg 埋点，不覆盖全局 Provider。不要额外用 `opentelemetry-instrument` 启动或另挂绕过隐私处理器的 exporter。

## 启动

从仓库根目录执行。先在部署环境设置已有的 `PG_PASSWORD`、`ADMIN_PASSWORD`、`HEALTHPILOT_IMAGE`，再增加以下两项秘密配置，不要提交到 Git：

- `HEALTH_AGENT_GRAFANA_PASSWORD`：Grafana 独立管理员密码。
- `HEALTH_AGENT_TELEMETRY_HMAC_KEY`：至少 32 字符的随机密钥，可用 `openssl rand -hex 32` 生成。API、Worker 必须一致，父进程会传给 MCP。轮换后用户/会话的遥测标识会改变。

```bash
uv sync --locked
docker compose -f docker-compose.yml -f ops/compose.runtime.yml -f ops/compose.observability.yml up -d
```

`HEALTHPILOT_IMAGE` 必须是包含本次代码、`uv.lock` 和前端产物的新镜像。重启旧镜像不会获得新实现。服务初始化阶段可能出现短暂导出失败，SDK/Collector 会按各自策略处理；启动后在后台查看导出状态。

Grafana 默认只绑定本机 `http://127.0.0.1:3000`，匿名访问和自助注册关闭。Loki、Tempo、Prometheus 和 Collector 不发布宿主机端口，只在 Compose 网络内部使用。后台继续使用原管理员鉴权，Grafana 使用独立账户。远程访问需经受控反向代理/TLS或 SSH 隧道；内部采集端点当前依赖可信容器网络，跨主机应配置 Collector 的 TLS/认证。不要将无鉴权的 Loki/Tempo/OTLP 端口直接暴露到公网。

部署文件预配三种 Grafana 数据源、日志→链路与链路→日志跳转、运行观测大盘、Prometheus 抓取与告警规则。告警规则加载不等于已发送通知，接收方仍由部署方配置。容器镜像使用固定版本；正式部署需按组织镜像更新与漏洞治理流程维护版本。

## 配置

| 配置 | 用途 |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | HTTP 基础地址，如 `http://otel-collector:4318`，自动追加 `/v1/traces`、`/v1/logs` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | 分信号覆盖，必须填写完整接收路径 |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | 本实现仅支持 `http/protobuf` |
| `OTEL_EXPORTER_OTLP_HEADERS` | 官方 exporter 认证头；支持 SDK 的分信号配置，不记录到日志 |
| `OTEL_EXPORTER_OTLP_CERTIFICATE` 等 SDK TLS 配置 | 远端 HTTPS 的 CA/客户端证书；MCP 是同一镜像内子进程，所指文件必须可读 |
| `OTEL_SDK_DISABLED=true` | 关闭 OTel 采集/导出，保留脱敏 stderr 日志 |
| `HEALTH_AGENT_TRACE_SAMPLE_RATIO` | 0–1，默认 1；父链路继承采样决定，不对日志做同比例采样 |
| `HEALTH_AGENT_TELEMETRY_HMAC_KEY` | 稳定脱敏用户和会话；缺失或不足 32 字符时不采集这两项标识 |
| `HEALTH_AGENT_LOKI_URL` | 后台查询的内部 Loki 地址；未设置时明确提示未配置 |
| `HEALTH_AGENT_LOKI_TOKEN` / `HEALTH_AGENT_LOKI_TENANT` | 可选查询认证和租户；不会发给浏览器 |
| `HEALTH_AGENT_GRAFANA_URL` | 浏览器可访问的 Grafana 基础地址 |
| `HEALTH_AGENT_TRUST_TRACEPARENT=1` | 仅可信入口剥离外部追踪头并重新生成时启用；默认不接受公网追踪上下文 |

没有配置 OTLP 端点时只输出脱敏 JSON，不创建新的本地日志数据库。上下文中无用户/任务的基础设施日志对应字段为空；未配置 HMAC 密钥不会虚构用户标识。SDK 配置完整地址/认证不会在后台状态接口泄露。

## 查询与历史迁移

`GET /api/v1/admin/logs` 沿用 V1 接口，底层改查 Loki；支持时间、服务、级别、事件、请求/任务/链路 ID、脱敏会话/用户标识和关键词。查询限定三十天、每页最多 200；游标改为纳秒时间和事件 ID，升级后应从第一页重新查询。同一纳秒的跨服务记录会补齐再分页；异常密集到超过 5000 条时明确要求缩小条件。

Loki 仅以服务名和服务命名空间建立低基数标签。request_id、trace_id、user_hash 等留在结构化元数据/JSON 中，不作为索引标签。关键词是区分大小写的日志行包含匹配，查询速度取决于窗口与数据量，不承诺未经压测的秒级 SLA。

`GET /api/v1/admin/traces/{trace_id}` 为兼容后台，返回最近三十天关联的业务日志；完整 OTel span（含数据库/HTTP 自动埋点）在 Tempo/Grafana 查看。日志页可导出当前页和已加载业务步骤。采样关闭或链路已过保留期时可能有日志而无完整 trace。

`GET /api/v1/admin/logs/status` 区分已配置的 traces/logs exporter、最近成功送达 Collector、失败批次/记录与 Loki 当前可达性。送达 Collector 不等于后端已经可查；Collector 队列积压应到 Grafana 查看。指标 `healthpilot_otel_export_*` 是当前 API 实例状态，不是所有 Worker 的合计。

旧 `hp_logs`、`runs/logs/central.sqlite3`、旧 SQLite spool 与 Docker `telemetry_api/telemetry_worker` 卷不删除；`app/telemetry_store.py` 只保留旧表查询代码供离线迁移。新实现不会自动清空或继续补传旧 spool，也不会把旧日志伪装成标准 OTel span。需要历史数据时先备份并单独迁移，旧版保留期清理程序已停止。新查询不与旧数据库混合。

## 可靠性与部署边界

应用 SDK 为每种信号使用 2048 条有界内存队列，每批最多 256、每秒触发导出。应用端进程崩溃、队列溢出或超过重试期限可能丢遥测；stderr 可由容器日志保留，但本配置不会自动把 stderr 再次采集，避免重复。应用审计记录仍使用业务事务，不能用运行日志替代。

Collector 使用 file_storage 持久发送队列（每 exporter 10000 批），指数退避重试；不在持久队列前叠加内存 batch processor。仅已被 Collector 接收并持久化的数据享有该层恢复能力；磁盘损坏、容量耗尽或永久拒收仍可能丢失。原始高基数/敏感信息已在应用侧导出前删除。

Loki 和 Tempo 配置 30 天保留期、命名持久卷；Collector、Loki、Tempo 以 UID 10001 运行，一次性初始化容器只设置卷根目录属主。Grafana 和 Prometheus 也有独立卷。现有业务数据库备份不包含这些新卷，需单独备份。`docker compose down -v` 会删除持久数据，不用于普通升级。

当前 Compose 是单机可部署配置，不包含多副本、对象存储、高可用、自动扩容或异地灾备。按用户要求本轮只进行依赖安装/锁定、语法/配置检查与前端构建，不运行测试、不启动基础设施、不做故障/容量演练。

参考：[Python SDK](https://opentelemetry.io/docs/languages/python/instrumentation/)、[Collector 持久队列](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/exporterhelper/README.md)、[Loki 原生 OTLP](https://grafana.com/docs/loki/latest/send-data/otel/)、[Grafana 日志与链路关联](https://grafana.com/docs/grafana/latest/datasources/tempo/configure-tempo-data-source/configure-trace-to-logs/)。
