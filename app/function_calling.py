"""把 MCP 工具连接到模型的 Function Calling 循环。"""
import json
from langchain_core.messages import ToolMessage


def mcp_tools_to_openai(tools: list) -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": t.input_schema}} for t in tools]


async def run_tool_loop(llm, tools: list[dict], call_tool, messages: list, max_steps: int = 4) -> tuple:
    messages = list(messages)
    trace = []
    bound = llm.bind_tools(tools)
    for _ in range(max_steps):
        response = await bound.ainvoke(messages)
        messages.append(response)
        if not response.tool_calls:
            return response, trace
        for call in response.tool_calls:
            try:
                result = await call_tool(call["name"], call["args"])
            except Exception as exc:
                result = {"error": str(exc)}
            trace.append({"tool": call["name"], "args": call["args"], "result": result})
            messages.append(ToolMessage(content=json.dumps(result, ensure_ascii=False), tool_call_id=call["id"]))
    return await llm.ainvoke(messages), trace
