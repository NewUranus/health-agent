# 随访流程规格

在现有 `checkin` 上补全随访：记录下次复查日期、到期提醒、用 LangGraph `interrupt` 做多轮随访问诊、记录用药和症状。优先级高于 `docs/DESIGN_V2.md` 中的 checkin 和 followup 章节。

不做：短信/微信推送、定时任务、医生审核队列。提醒只在用户打开页面时出现。

## 确定性规则（app/followup.py，纯函数，不调模型）

```python
INTERVAL_DAYS = {"high": 30, "intermediate": 90, "low": 365}

def make_plan(level: str, today: date, symptoms: bool = False) -> dict
```
返回 `{"due_date": "YYYY-MM-DD", "interval_days": int, "level": level}`。有症状时间隔取 `min(30, 原间隔)`。

```python
def plan_status(plan: dict | None, today: date) -> dict
```
- `plan` 为空：`{"status": "none"}`
- 否则返回 `{"status", "due_date", "days_until", "level"}`，`days_until = due_date - today`：`> 0` 为 `upcoming`；`-14 <= d <= 0` 为 `due`；`< -14` 为 `overdue`

```python
def classify_adherence(text: str) -> str    # "poor" | "good" | "unknown"
```
先判 poor（没、忘、漏、偶尔、不太、很少、停），再判 good（按时、都吃、坚持、做到、不错、挺好、还行、有），否则 unknown。

```python
def reports_symptoms(text: str) -> bool
```
去掉首尾空白和标点后，以 `没`、`无`、`都没`、`还好`、`正常`、`不` 开头返回 False，否则 True。

```python
def parse_smoking(text: str) -> bool
```
包含 `戒`、`不`、`没` 返回 False，否则 True。

当前日期只能通过 `app/clock.py` 的 `today() -> date`（Asia/Shanghai）获取，调用时写 `clock.today()`，测试会替换它。

## 长期记忆（app/memory.py）

Store 的 `("users", user_id)` 下新增 key `followup`，值是 `make_plan` 的结果。

`history.records` 每条增加 `kind`（`"assessment"` 或 `"checkin"`）；checkin 记录另外带 `adherence`（字符串）、`symptoms`（bool）、`bp_updated`（bool）。

`followup` 节点（首次评估）和 `checkin` 节点完成时都要写入新计划。

`followup` 节点的回复在随访计划一段增加一行 `下次复查日期：YYYY-MM-DD`，事件数据增加 `due_date`。

## 多轮问诊（checkin 节点）

仍然由 supervisor 路由到 `checkin_agent`，节点名仍是 `checkin`，在节点内部依次调用 `interrupt()`：

| key | 何时问 | 问题 |
|---|---|---|
| `bp` | 触发这一轮没解析出 `sbp` | `请告诉我最近一次测量的血压，例如 130/85。` |
| `adherence` | 总是问 | 档案 `bp_treated` 为真：`最近有按时服用降压药吗？`；否则：`最近在限盐、运动这些方面做得怎么样？` |
| `smoking` | 档案 `smoker` 为真 | `目前还在吸烟吗？` |
| `symptoms` | 总是问 | `最近有没有头晕、胸闷、气短这些不适？` |

`interrupt()` 的值：`{"key", "question", "step", "total"}`，`step` 从 1 开始，`total` 是这次要问的题数。

### 回答处理

**每条回答都要先检查，顺序不能变：**

1. `check_red_flags(回答)` 命中：立刻结束，回复和 `emergency` 节点相同的急症文案（含 120），**不调用工具、不写记录、不改计划**。事件数据 `{"red_flags": [...]}`。恢复执行不会重新经过 `safety_check`，所以必须在这里判断
2. 回答包含 `取消`：立刻结束，回复 `已取消本次随访，记录没有变化。`，不调用工具、不写记录。事件数据 `{"cancelled": true}`
3. 按 key 处理：
   - `bp`：用 `parse_profile` 取 sbp/dbp。解析不到就再问一次，问题前加 `没有识别出血压。`，`step` 不变。第二次还解析不到，用档案原值继续，`bp_updated=False`
   - `adherence`：`classify_adherence`
   - `smoking`：`parse_smoking`，结果更新档案
   - `symptoms`：`reports_symptoms`

节点内的副作用（MCP 调用、写 Store）必须放在所有 `interrupt()` 之后，因为恢复时节点会从头重新执行。

### 完成后

用更新后的档案经 MCP 调 `cvd_risk`，写记录和新计划，更新档案。事件数据：`previous_risk`、`current_risk`、`delta`、`tool_calls`、`adherence`、`symptoms`、`bp_updated`、`due_date`。

回复（固定字符串会被测试检查）：

```
## 随访对比
上次风险：13.5%
本次风险：11.8%
历次风险：13.5% → 11.8%
依从情况：良好            ← poor=需要改进，good=良好，unknown=未明确
不适症状：无              ← 或"有"
{原有的下降/不变/上升话术，高风险追加"目前仍属高风险…"}
{有症状："你提到了不适症状，建议尽快就诊，不要等到下次复查。"}
{bp_treated 且 poor："请不要自行停药或减药，按医嘱服药；有困难请咨询医生。"}
{bp_updated 为假："血压没有更新，以上按上次的数值计算。"}
下次复查日期：YYYY-MM-DD
{risk note}

> 仅为技术演示，不构成医疗建议。
```

## 事件流（app/agent.py）

`_stream` 开始时用 `graph.aget_state(config, subgraphs=True)` 判断这个 thread 是否停在 interrupt 上（包括子图里的任务）：

- 是：用 `Command(resume=message)` 恢复，**不跑** safety_check、intake、supervisor
- 否：按现在的方式从头运行

`astream` 产出 `__interrupt__` 时，产出事件：

```json
{"type": "node", "node": "checkin_question", "agent": "checkin_agent", "ms": 0, "data": {"key": "adherence", "step": 1, "total": 2}}
```

然后 `{"type": "message", "content": 问题}`、`{"type": "done"}`。每轮仍然恰好一个 message。`OWNERS` 增加 `checkin_question`。

## HTTP

`GET /api/followup?user_id=xxx`：返回 `plan_status(存储的计划, clock.today())`，另外带 `last_risk_percent`（有历史时）。

## 前端

- 页面加载、每轮结束、新会话、清除身份后都请求 `/api/followup?user_id=...`（请求失败时静默忽略）
- `upcoming`：概览里显示 `下次复查：YYYY-MM-DD`
- `due`：显示带 `role="alert"` 的提醒 `已到复查时间`，和按钮 `开始复查`
- `overdue`：提醒文案 `复查已逾期 N 天`（N 为 `-days_until`），和按钮 `开始复查`
- 点 `开始复查` 发送 `我来复查`
- 上一个事件是 `checkin_question` 时：不显示提醒；状态文字（`role="status"`）显示 `请回答上面的随访问题（回复"取消"可退出）`
- 轨迹卡片：`checkin_question` 的节点中文名 `随访问诊`，显示 `第 {step}/{total} 题`。`checkin` 卡片显示依从情况和下次复查日期；`data.red_flags` 非空时显示 warning `发现急症信号，随访已中止。`
