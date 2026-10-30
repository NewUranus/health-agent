# HealthPilot

心血管健康评估 Agent 演示项目。完整规格见 `docs/DESIGN.md`，实现必须与之一致。

## 规则

- **禁止修改 `tests/`、`corpus/`、`eval/cases.jsonl`。** 测试不过就改实现。
- Agent 调用健康工具必须经过 MCP（`app/mcp_client.py` → `mcp_server/server.py` 子进程），不能直接 import `tools_core` 绕过。
- 急症护栏、风险计算、引用校验、随访规则是确定性代码，不能交给 LLM。
- 不在代码或日志里输出 `.env` 中的密钥。
- 代码注释、提示词、界面文案用中文；标识符用英文。

## 命令

```bash
uv sync
uv run pytest -q
uv run uvicorn app.main:app --port 8000          # 真实模式，读 .env
HEALTH_AGENT_MOCK=1 uv run uvicorn app.main:app --port 8000
uv run python -m eval.run_eval --mock
```
