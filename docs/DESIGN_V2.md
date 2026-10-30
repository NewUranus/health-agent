# HealthPilot V2：对齐 JD 的扩展

本文件是对 `docs/DESIGN.md` 的扩展。**两者冲突时以本文件为准。** 被本文件完全取代的 V1 章节：「状态图」「Agent 与事件流」「HTTP 接口」「评测」。其余章节（纯函数、MCP、急症护栏、问诊解析、检索、引用校验、前端）继续有效，并按下文增补。

## JD 对照

| JD 要求 | 落点 |
|---|---|
| 任务分解、多步推理 | supervisor 路由 + 评估→干预→随访的多 Agent 接力 |
| 工具调用（Function Calling） | `app/function_calling.py`：问答 Agent 的工具循环 |
| 长短期记忆 | 短期：LangGraph checkpointer；长期：LangGraph Store（跨会话档案与评估历史） |
| LangGraph 多 Agent 编排、状态管理 | 主图 + 5 个子图 |
| 问诊 / 健康干预 / 随访流程 | intake_agent / intervention_agent / followup_agent |
| 世界模型能力封装为 MCP | 新增 `simulate_intervention`（干预效果模拟）、`project_trajectory`（健康轨迹推演） |
| 低延迟对接 | MCP 长连接复用、并行调用、工具结果缓存、每节点耗时 |
| 医学 RAG、术语库、降低幻觉 | BM25 + 向量 RRF 融合、`data/terms.json` 术语归一、引用校验 |
| Prompt 工程 | `app/prompts/*.md`，带版本号，版本写入事件 |
| 评测体系 | 任务成功率、规划准确率、医学事实性、时延、交互体验 |
| FastAPI、Git、Docker、CI/CD | `docker-compose.yml`、`.github/workflows/ci.yml` |
| PostgreSQL | 配置 `DATABASE_URL` 时，checkpointer 和 Store 都用 Postgres |
| Dify/Coze 对接 | `/api/tools/*` REST 接口，FastAPI 自动生成 OpenAPI，可作为自定义工具导入 |

## 新增 MCP 工具（app/tools_core.py + mcp_server/server.py）

风险类工具共用 8 个基础参数：`sex, age, total_chol_mmol, hdl_mmol, sbp, bp_treated, smoker, diabetes`。

```python
def simulate_intervention(<8 个基础参数>, quit_smoking: bool = False,
                          sbp_target: int | None = None,
                          total_chol_target: float | None = None) -> dict
```

用同一个 Framingham 公式计算改变前后的风险。返回：

- `baseline`：当前风险（%，2 位小数）
- `simulated`：改变后风险（%，2 位小数）
- `absolute_reduction`：`baseline - simulated`，2 位小数
- `relative_reduction_percent`：相对下降百分比，1 位小数
- `changes`：中文描述列表，如 `["戒烟", "收缩压降至 120 mmHg"]`
- `note`：必须说明这是基于风险公式的估算，不代表个体实际获益

```python
def project_trajectory(<8 个基础参数>, years: list[int] | None = None) -> dict
```

`years` 默认 `[0, 5, 10]`。其他因素不变，只增加年龄。返回 `{"points": [{"years", "age", "risk_percent"}], "note"}`。年龄超过 74（模型适用上限）的点直接跳过。`note` 必须说明"假设其他因素不变"。

缓存：`app/mcp_client.py` 对相同工具名和相同参数的调用做进程内 LRU 缓存（`maxsize=256`），事件的 `tool_calls` 项增加 `cached: bool`。

## 术语归一（app/terms.py + data/terms.json）

`data/terms.json` 由实现方创建，格式 `{"俗称": ["标准术语", ...]}`，至少包含：

高压→收缩压、低压→舒张压、好胆固醇→高密度脂蛋白 HDL、坏胆固醇→低密度脂蛋白 LDL、心梗→心肌梗死、中风→脑卒中、抽烟→吸烟、喝酒→饮酒、血糖高→糖尿病、三高→高血压 血脂 糖尿病。

`expand_query(text: str) -> str`：返回原文加上命中俗称对应的标准术语，用空格拼接；未命中时原样返回。`Retriever.search` 内部先调用它。

## 混合检索（app/rag.py）

- `rrf_merge(rankings: list[list[str]], k: int = 60) -> list[str]`：RRF 融合，分数 `Σ 1/(k + rank)`，rank 从 1 开始，按分数降序，同分时按首次出现顺序
- `Retriever.from_dir(path, embedder=None)`：`embedder` 为可选函数 `list[str] -> list[list[float]]`。有 embedder 时做 BM25 + 向量余弦的 RRF 融合；没有时只用 BM25（Mock 模式与测试）
- 真实模式用 OpenAI 兼容的 embeddings 接口，模型取 `QWEN_EMBEDDING_MODEL`（未配置或调用失败时退回纯 BM25，并记日志）

## 长期记忆（app/memory.py）

使用 LangGraph 的 `BaseStore`：默认 `InMemoryStore`；配置了 `DATABASE_URL` 时用 `AsyncPostgresStore`，checkpointer 同样换成 `AsyncPostgresSaver`（都在 lifespan 中 `setup()`）。

命名空间 `("users", user_id)`，两个 key：

- `profile`：最新健康档案
- `history`：`{"records": [{"at": ISO时间, "risk_percent", "level", "sbp", "dbp", "total_chol", "hdl", "smoker"}]}`

## 状态图（取代 V1 同名章节）

主图把子 Agent 作为子图（`StateGraph(...).compile()` 后作为节点加入）。**每条消息都从 START 跑一遍。**

```
START → safety_check
safety_check ─命中急症→ emergency → END
safety_check ─否则→ intake → supervisor
supervisor 路由：
  intake_agent        → [ask_missing] → END
  assessment_agent    → [risk_assess] → intervention_agent → [retrieve → advise → verify ⟲] → followup_agent → [followup] → END
  simulation_agent    → [simulate] → END
  checkin_agent       → [checkin] → END
  qa_agent            → [answer] → END
```

节点归属（事件的 `agent` 字段）：`safety_check`、`emergency`、`intake`、`supervisor` 属于 `orchestrator`；其余属于上图的子 Agent。`followup` 属于 `followup_agent`，`retrieve/advise/verify` 属于 `intervention_agent`。

### intake（每轮都执行）

合并顺序（后者覆盖前者）：长期记忆 `profile` → 本会话 `profile` → 本轮解析结果。事件数据增加 `from_memory`：值来自长期记忆、且本会话和本轮都没提供的字段名列表（排序后返回）。真实模式下用 LLM 抽取补充规则解析没拿到的字段，但规则解析到的字段优先。

**假设性提问不改档案：** 本轮文本命中 `如果|假如|要是` 时，本轮解析结果不合并（"如果我不吸烟"不能把档案改成不吸烟）。

### supervisor（路由优先级从上到下，确定性规则）

1. 缺必填字段 → `intake_agent`，理由"信息不全"
2. 有评估历史，且文本命中 `如果|假如|要是|能降|降到` → `simulation_agent`
3. 有评估历史，且文本命中 `随访|复查|复诊|最近血压|最新血压` → `checkin_agent`
4. 本会话 `stage != "assessed"` 且长期记忆没有评估历史 → `assessment_agent`
5. 本会话 `stage != "assessed"`，有历史，本轮解析出至少一个新字段 → `assessment_agent`
6. 其余 → `qa_agent`

事件数据：`{"route": str, "reason": str}`。真实模式下规则 6 可以改用 LLM 做意图分类（取值限定在上述路由内，失败回退 `qa_agent`），规则 1–5 不交给 LLM。

"有评估历史"指长期记忆 `history.records` 非空。

### risk_assess

在 V1 基础上增加调用 `project_trajectory`，工具调用用 `asyncio.gather` 并行执行。

### simulate

规则解析本轮文本：`血压降到(\d+)` → `sbp_target`；`胆固醇降到(\d+(\.\d+)?)` → `total_chol_target`；包含`戒烟`或`不抽烟`或`不吸烟` → `quit_smoking=True`。都没解析到时，默认模拟 `sbp_target = 档案 sbp - 10`。

经 MCP 调用 `simulate_intervention`。事件数据 `tool_calls`。回复必须包含：

```
当前风险：{baseline:.1f}%
模拟后风险：{simulated:.1f}%（绝对下降 {absolute_reduction:.1f} 个百分点）
```

并带上工具返回的 `note` 和免责声明。

### checkin（随访流程）

用合并后的档案经 MCP 重新计算 `cvd_risk`，与 `history.records` 最后一条比较，然后把新记录追加到历史、更新 `profile`。事件数据：`previous_risk`、`current_risk`、`delta`、`tool_calls`。回复必须包含：

```
上次风险：{previous:.1f}%
本次风险：{current:.1f}%
```

风险下降、不变、上升时分别给出鼓励或提醒就医的话。

### followup

V1 的规则不变，另外把档案和本次评估写入长期记忆。

### answer（Function Calling）

真实模式：检索 top 4 + `run_tool_loop`，模型可调用全部 MCP 工具。Mock 模式：模板拼出带引用的回答。事件数据：`chunks`、`tool_calls`（Mock 下为空列表）。

### 评估回复格式

在 V1 格式的"血压分级 / BMI"之后、"干预建议"之前增加：

```
## 风险轨迹
5年后（60岁）：17.2%
10年后（65岁）：21.4%
```

只显示 5 年和 10 年的点。被跳过的点不显示；两个点都不存在时（如 72 岁）省略整段。

## Function Calling（app/function_calling.py）

```python
def mcp_tools_to_openai(tools: list) -> list[dict]
```
把 MCP `list_tools` 的工具（`.name`、`.description`、`.input_schema`）转成 OpenAI function 格式：`{"type": "function", "function": {"name", "description", "parameters"}}`。

```python
async def run_tool_loop(llm, tools: list[dict], call_tool, messages: list, max_steps: int = 4) -> tuple
```
- 用 `llm.bind_tools(tools)` 绑定，循环 `await bound.ainvoke(messages)`
- 返回的 `AIMessage` 带 `tool_calls` 时，对每个调用执行 `await call_tool(name, args)`，结果以 JSON 字符串放进 `ToolMessage(tool_call_id=...)` 追加到 messages
- 工具抛异常时不中断，把错误文本作为 `ToolMessage` 内容交还模型
- 没有 `tool_calls` 或达到 `max_steps` 时结束。达到上限时最后一次调用不再绑定工具（用 `llm.ainvoke`）以强制给出文字回答
- 返回 `(final_ai_message, trace)`，`trace` 每项 `{"tool", "args", "result"}`；出错的项 `result` 为 `{"error": str}`

## Prompt 工程（app/prompts/）

每个提示词一个 `.md` 文件（`advise.md`、`answer.md`、`extract.md`、`intent.md`）。第一行 `<!-- version: x.y -->`。`app/prompts/__init__.py` 提供 `load_prompt(name) -> Prompt`，`Prompt` 有 `.text`、`.version`。advise 和 answer 节点的事件数据里带 `prompt_version`。

## 事件流（取代 V1 同名章节）

- `{"type": "node", "node": 名称, "agent": 所属 Agent, "ms": 耗时毫秒(int), "data": {...}}`
- `{"type": "message", "content": ...}`：每轮恰好一个
- `{"type": "done"}`：最后一个
- 出错：`{"type": "error", "message": ...}` 再 `done`

用 `graph.astream(..., stream_mode="updates", subgraphs=True)`。子图本身那条汇总更新不要产生事件，只为叶子节点产生事件。`ms` 是该事件距上一事件的时间差即可。

每轮把全部事件追加写入 `runs/traces/<thread_id>.jsonl`（目录不存在就创建；`runs/` 要加入 `.gitignore`）。

## HTTP 接口（取代 V1 同名章节）

V1 接口保留，变化：

- `POST /api/chat` body 增加可选 `user_id`，缺省时等于 `thread_id`
- 新增 `POST /api/tools/{cvd_risk|bmi|bp_category|simulate_intervention|project_trajectory}`：JSON 参数，经 MCP 转发，返回工具结果；参数错误返回 400。每个工具单独声明路由和 Pydantic 请求模型，这样生成的 OpenAPI 可以导入 Dify / Coze

## 前端增补

- 用 `localStorage` 保存 `user_id`（读写包 try/catch），"新会话"只换 `thread_id`，不换 `user_id`；再加一个"清除身份"按钮换 `user_id`
- 轨迹卡片显示所属 Agent（配色区分）和耗时；supervisor 卡片显示路由和理由；`from_memory` 非空时显示"来自长期记忆"
- 增加两个示例按钮："如果我把血压降到120，风险能降多少？"、"我来复查，最近血压130/85"
- 右侧面板可折叠，默认展开（客户演示时可以收起来）

## 评测（取代 V1 同名章节）

`python -m eval.run_eval [--mock] [--judge]` 读 `eval/cases.jsonl`。`red_flag`、`parse` 两类同 V1。`e2e` 用例字段：

- `turns`：用户消息列表，同一 thread、同一 user
- `expect_risk`：期望风险。与整个用例中最后一次风险类工具调用比较：`cvd_risk` 取 `risk_percent`，`simulate_intervention` 取 `simulated`；`null` 表示不检查
- `expect_emergency`：bool
- `expect_routes`：每轮的期望路由（急症轮写 `"emergency"`）

指标（写入 `eval/results/<时间戳>.json` 并打印）：

| 指标 | 定义 |
|---|---|
| 任务成功率 | 风险相差 < 0.05，急症用例回复含 120 且无工具调用，全部满足的比例 |
| 规划准确率 | 实际路由序列与 `expect_routes` 完全相等的比例 |
| 医学事实性 | 所有 verify 事件中首次通过的比例，以及 fallback 次数 |
| 急症召回 / 误报 | 来自 `red_flag` 类 |
| 端到端时延 | 每轮耗时的 p50、p95（秒） |
| 交互体验 | 仅 `--judge`：LLM 按清晰度、共情、可执行性各打 1–5 分，取平均；Mock 模式忽略此项 |

## 部署（已写好，不要改）

`Dockerfile`、`docker-compose.yml`、`Caddyfile`、`docs/DEPLOY.md` 由设计方维护。`.github/workflows/ci.yml` 由实现方创建：push 和 PR 时用 `astral-sh/setup-uv` 跑 `uv sync`、`uv run pytest -q`、`uv run python -m eval.run_eval --mock`。

## 不做的事

China-PAR、检验报告图片识别、回放模式、账号体系都不在本轮范围内。
