# HealthPilot 设计文档

一个心血管健康评估 Agent 的演示项目：多轮问诊收集信息 → 急症护栏 → 通过 MCP 调用风险模型 → 检索指南 → 生成带引用的干预建议 → 引用校验 → 随访计划。配一个简单的网页前端，右侧实时展示 Agent 每一步的轨迹。

> 仅为技术演示，不构成医疗建议。语料是对公开指南要点的简短摘要。

## 技术栈

- Python 3.12+，用 uv 管理依赖
- FastAPI + uvicorn（后端，SSE 流式推送）
- LangGraph（状态机编排 + `MemorySaver` checkpointer 实现多轮会话状态）
- MCP 官方 Python SDK 2.x（`from mcp.server.mcpserver import MCPServer`，v2 中 FastMCP 已改名为 MCPServer；客户端用 `mcp.ClientSession` + `mcp.client.stdio.stdio_client`，结果的错误标记是 `result.is_error`），stdio 传输；Agent 通过 MCP 客户端调用工具，不允许绕过 MCP 直接 import 工具函数
- 检索：`rank-bm25` + `jieba` 分词（无需下载向量模型）
- LLM：通义千问 OpenAI 兼容接口（`langchain-openai` 的 `ChatOpenAI`），读 `.env` 中的 `QWEN_API_KEY` / `QWEN_BASE_URL` / `QWEN_MODEL`
- 前端：`web/` 下的纯 HTML + CSS + 原生 JS，无构建步骤，由 FastAPI 静态托管

## 运行模式

- `HEALTH_AGENT_MOCK=1`：不调用 LLM。信息抽取只用规则解析，建议和问答由模板根据检索结果拼出。测试、离线演示都用这个模式。
- 默认（真实模式）：LLM 负责信息抽取（与规则解析结果合并，规则结果优先用于数字字段）、生成追问、生成建议、回答追问。

两种模式下急症护栏、风险计算、引用校验、随访规则都完全一样，都是确定性代码。

## 目录结构

```
app/
  main.py        FastAPI 应用（create_app()），路由见下文
  agent.py       HealthAgent：组装图，提供 async stream(thread_id, message) 产出事件
  graph.py       LangGraph 状态图与节点
  intake.py      parse_profile(text) 规则解析、REQUIRED_FIELDS、缺失字段计算
  safety.py      check_red_flags(text)
  rag.py         Chunk、Retriever
  verify.py      verify_advice(text, allowed_ids)
  tools_core.py  纯函数：framingham_cvd_risk、bmi、bp_category
  mcp_client.py  MCP stdio 客户端封装（启动 mcp_server 子进程，list_tools / call_tool）
  llm.py         make_llm()、Mock 实现
mcp_server/
  server.py      MCPServer("health-tools")，`python -m mcp_server.server` 以 stdio 启动
corpus/*.md      指南摘要语料，每个文件一个 chunk（已写好，不要改内容）
web/             index.html、app.js、style.css
eval/
  cases.jsonl    评测用例（已写好）
  run_eval.py    `python -m eval.run_eval [--mock]`
tests/           验收测试（已写好，禁止修改）
```

## 纯函数（app/tools_core.py）

### framingham_cvd_risk

```python
def framingham_cvd_risk(sex: str, age: int, total_chol_mmol: float, hdl_mmol: float,
                        sbp: int, bp_treated: bool, smoker: bool, diabetes: bool) -> dict
```

D'Agostino 2008 Framingham 一般心血管病 10 年风险（实验室版）。输入胆固醇为 mmol/L，内部乘以 38.67 换算为 mg/dL。

| 系数 | 男 | 女 |
|---|---|---|
| ln(年龄) | 3.06117 | 2.32888 |
| ln(总胆固醇) | 1.12370 | 1.20904 |
| ln(HDL) | -0.93263 | -0.70833 |
| ln(收缩压)，未治疗 | 1.93303 | 2.76157 |
| ln(收缩压)，已治疗 | 1.99881 | 2.82263 |
| 吸烟 | 0.65451 | 0.52873 |
| 糖尿病 | 0.57367 | 0.69154 |
| 均值 | 23.9802 | 26.1931 |
| 基线生存率 S0 | 0.88936 | 0.95012 |

风险 = 1 − S0 ^ exp(Σβx − 均值)，乘 100 后保留 2 位小数。系数已与 R 包 CVrisk 的 `frs_coef` 核对一致。

- `sex` 取 `"male"` / `"female"`，其他值抛 `ValueError`
- 年龄超出 30–74 抛 `ValueError`（模型适用范围）
- 返回 `{"risk_percent": float, "level": "low"|"intermediate"|"high", "model": "Framingham 2008 general CVD", "note": str}`
- 分级：<10 为 low，10 ≤ r < 20 为 intermediate，≥20 为 high
- `note` 需说明该模型基于美国人群，可能高估中国人群风险，国内推荐 China-PAR

### bmi

`bmi(height_cm, weight_kg) -> {"bmi": float(1 位小数), "category": str}`，中国标准（WS/T 428-2013）：<18.5 偏瘦，18.5–23.9 正常，24.0–27.9 超重，≥28 肥胖。按保留 1 位小数之后的值分级。

### bp_category

`bp_category(sbp, dbp) -> {"category": str}`，按《中国高血压防治指南（2018）》，收缩压和舒张压分别定级后取较高一级：

| 分级 | 收缩压 | 舒张压 |
|---|---|---|
| 正常 | <120 | <80 |
| 正常高值 | 120–139 | 80–89 |
| 1级高血压 | 140–159 | 90–99 |
| 2级高血压 | 160–179 | 100–109 |
| 3级高血压 | ≥180 | ≥110 |

## MCP 服务端（mcp_server/server.py）

`MCPServer("health-tools")`，暴露三个工具，参数名与上面纯函数一致：`cvd_risk`、`bmi`、`bp_category`。工具返回 JSON 对象（dict）。参数非法时返回 MCP 错误（isError），不让进程崩溃。

## 急症护栏（app/safety.py）

`check_red_flags(text: str) -> list[str]`，返回命中的急症类别名，未命中返回空列表。纯关键词 / 正则规则，不调用 LLM。至少覆盖：

- `chest_pain`：胸痛、胸口剧痛、胸口压榨感、心口疼（伴或不伴出汗）
- `breathing`：喘不上气、呼吸困难、憋气
- `stroke`：口角歪斜、一侧/半边身体无力或麻木、说话不清、言语不清
- `syncope`：晕倒、昏迷、意识不清、失去意识
- `self_harm`：自杀、不想活、轻生

"没有胸痛"、"无胸痛"、"不胸痛"这类否定表述不能命中。

## 问诊信息（app/intake.py）

`parse_profile(text: str) -> dict`，规则解析中文自由文本，只返回识别到的字段：

| 字段 | 类型 | 示例输入 |
|---|---|---|
| sex | "male"/"female" | 男 / 男性 / 女士 / 女 |
| age | int | 55岁 / 今年55 |
| height_cm | float | 身高172 / 172cm / 1.72米 |
| weight_kg | float | 体重80kg / 80公斤 / 160斤（换算为 80.0） |
| sbp, dbp | int | 血压140/90 / 高压140低压90 |
| total_chol | float | 总胆固醇5.5 / TC 5.5 |
| hdl | float | HDL 1.3 / 高密度脂蛋白1.3 / 高密度1.3 |
| smoker | bool | 吸烟/抽烟 → True；不吸烟/不抽烟/戒烟了/从不抽烟 → False |
| diabetes | bool | 有糖尿病 → True；没有糖尿病/无糖尿病/不是糖尿病 → False |
| bp_treated | bool | 在吃降压药/服用降压药 → True；没吃降压药/未服降压药/不吃降压药 → False |

否定必须优先于肯定判断（"不吸烟"不能因包含"吸烟"被判为 True）。

`REQUIRED_FIELDS = ["sex", "age", "sbp", "total_chol", "hdl", "smoker", "diabetes", "bp_treated"]`，`missing_fields(profile) -> list[str]` 返回缺失的必填字段（保持上面的顺序）。身高体重可选，有就算 BMI。

## 检索（app/rag.py）

- `Chunk`：dataclass，字段 `id`（文件名去扩展名）、`title`、`source`、`text`、`score`
- `Retriever.from_dir(path)`：读取 `corpus/*.md`。文件开头是 `---` 包围的简易 front matter（`title:`、`source:` 两行），其后为正文
- `Retriever.search(query, k=4) -> list[Chunk]`：BM25（`rank_bm25.BM25Okapi`，title 与正文一起建索引），按分数降序，过滤掉分数 ≤ 0 的结果
- 分词：先转小写，取 `jieba.lcut_for_search` 的结果（丢掉纯标点/空白），再额外追加文本中的每个汉字单字。单字能保证"减盐"这类短查询也能命中"食盐""钠盐"

## 引用校验（app/verify.py）

建议文本中用 `[cite:chunk_id]` 标注引用。`verify_advice(text, allowed_ids) -> VerifyResult`，`VerifyResult` 有 `ok: bool`、`problems: list[str]`。规则：

1. 至少有一个引用
2. 所有引用的 id 都在 `allowed_ids` 内（即本轮检索到的 chunk）
3. 不得出现具体药物剂量：正则匹配数字后跟 `mg`、`毫克`、`μg`、`微克`（大小写不敏感）
4. 文本按行（或按 `- ` / 数字编号开头的条目）拆分后，每条建议条目都要至少带一个引用；空行和标题行（以 `#` 开头）不计

不通过时 `problems` 用中文描述每个问题。

## 状态图（app/graph.py）

每条用户消息都从 START 跑一遍图；会话状态由 checkpointer 按 `thread_id` 持久化（`MemorySaver`），所以第二轮不必重复年龄性别。

状态字段（TypedDict）：`messages`、`profile`（dict，逐轮合并）、`red_flags`、`missing`、`assessment`（工具结果）、`retrieved`（chunk 列表）、`advice`、`verify`、`followup`、`stage`（`"intake" | "assessed"`）、`reply`（本轮回复）。

节点与路由：

```
START → safety_check
safety_check ──命中急症──→ emergency → END
safety_check ──stage=assessed──→ answer → END          （评估后的追问）
safety_check ──否则──→ intake
intake ──还缺字段──→ ask_missing → END                  （追问缺失信息）
intake ──齐了──→ risk_assess → retrieve → advise → verify
verify ──不通过且重试 < 2──→ advise
verify ──通过，或重试用完（改用安全模板）──→ followup → END
```

- `emergency`：回复必须包含"120"，建议立即就医；不调用任何工具
- `ask_missing`：用中文列出缺失项，问用户补充。缺失项名称用中文：性别、年龄、血压、总胆固醇、高密度脂蛋白（HDL）、是否吸烟、是否有糖尿病、是否在服降压药
- `risk_assess`：通过 MCP 客户端依次调用 `cvd_risk`、`bp_category`（有 dbp 时；没有 dbp 用 sbp 与 0）、`bmi`（有身高体重时）
- `retrieve`：根据风险因素拼查询（如血压偏高 → 限盐、降压；吸烟 → 戒烟；BMI 超重 → 减重；总是带上运动、膳食），合并多次检索去重，最多 6 个 chunk
- `advise`：真实模式用 LLM，提示词要求只依据检索到的 chunk 写 3–5 条建议，每条带 `[cite:id]`，不得给药物剂量；Mock 模式用模板，从检索结果里每个 chunk 生成一条建议
- `verify`：调用 `verify_advice`；两次重试都不通过时用安全模板（Mock 模板）替换，并在事件里标出 `fallback: true`
- `followup`：确定性规则：high → 1 个月内就诊复查；intermediate → 3 个月复查血压血脂；low → 12 个月常规体检。存到 `followup`，之后 stage 设为 `"assessed"`
- `answer`：评估后的自由问答。检索用户问题 top 4，真实模式用 LLM（可绑定 MCP 工具做 Function Calling），回答带 `[cite:id]`；Mock 模式用模板拼出带引用的回答

### 评估完成时的回复格式

`followup` 节点后的最终回复必须包含以下几部分（测试会检查其中的固定字符串）：

```
## 评估结果
10年心血管病风险：{risk_percent:.1f}%（{level 的中文：低/中/高}风险）
血压分级：{category}
BMI：{bmi}（{category}）              ← 有身高体重时
## 干预建议
- ...[cite:xxx]
## 随访计划
...
> 仅为技术演示，不构成医疗建议。
```

## Agent 与事件流（app/agent.py）

`HealthAgent(mock: bool)`，`async def stream(thread_id: str, message: str)` 异步产出事件 dict：

- `{"type": "node", "node": <节点名>, "data": {...}}`：每个节点执行完产出一个。`data` 至少包含：
  - safety_check：`red_flags`
  - intake：`profile`、`missing`
  - risk_assess：`tool_calls`（列表，每项 `{"tool", "args", "result", "via": "mcp"}`）
  - retrieve：`chunks`（每项 `id`、`title`、`source`、`score`）
  - verify：`ok`、`problems`、`attempt`、`fallback`
  - followup：`followup`
  - answer：`chunks`
- `{"type": "message", "content": <本轮回复全文>}`：每轮恰好一个
- `{"type": "done"}`：最后一个事件
- 出错时产出 `{"type": "error", "message": ...}` 再 `done`

MCP 子进程在应用启动时建立一次连接并复用（FastAPI lifespan），不要每次调用都重启。

## HTTP 接口（app/main.py）

`create_app()` 返回 FastAPI 实例，模块级 `app = create_app()`。

- `GET /`：返回 `web/index.html`
- `GET /static/*`：`web/` 目录静态文件
- `GET /api/health`：`{"ok": true, "mock": bool}`
- `POST /api/chat`，body `{"thread_id": str, "message": str}`：返回 `text/event-stream`，每个事件一行 `data: <json>`，事件间空行分隔

启动：`uv run uvicorn app.main:app --port 8000`。

## 前端（web/）

单页、中文、浅色为主、适配手机宽度。

- 左侧聊天区：消息气泡，助手回复做简易 Markdown 渲染（标题、列表、引用块、粗体），`[cite:id]` 渲染成可点击的小角标，点击在右侧高亮对应来源
- 右侧"Agent 轨迹"面板：按顺序展示每个节点事件卡片（节点名中文 + 关键数据），MCP 工具调用显示参数和结果，检索结果显示标题和来源，校验结果显示通过/问题
- 顶部：健康档案卡（已收集的字段）和风险仪表（低/中/高配色）
- 输入框 + 发送；"新会话"按钮（生成新 thread_id）；两个示例按钮："男，55岁，血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药" 和 "突然胸口剧痛，出冷汗"
- 用 `fetch` 读 SSE 流（POST 不能用 EventSource），边收边渲染
- 顶部显示 Mock/真实模式标记（来自 `/api/health`）

## 评测（eval/）

`python -m eval.run_eval [--mock]` 读取 `eval/cases.jsonl`，每行一个用例：

- `{"kind": "red_flag", "text": ..., "expect": true|false}`：只测 `check_red_flags`
- `{"kind": "parse", "text": ..., "expect": {字段: 值}}`：测 `parse_profile`，期望字段全部匹配才算通过
- `{"kind": "e2e", "turns": [...], "expect_risk": float | null, "expect_emergency": bool}`：走完整 HealthAgent。通过条件：急症用例回复含 "120" 且没有调用工具；评估用例的风险值与期望相差 < 0.05、最终 verify ok（不是 fallback）

输出每类的通过率，以及 e2e 的：引用有效率（verify 首次通过的比例）、fallback 次数、平均端到端时延（秒）。结果同时写到 `eval/results/<时间戳>.json`。
