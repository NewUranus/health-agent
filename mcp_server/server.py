"""通过标准输入输出暴露确定性健康工具和公开网页工具。"""
from mcp.server.mcpserver import MCPServer
from app import tools_core
from app.telemetry import bind, extract, span, runtime
from app.web_tools import search_web, read_web_document

server = MCPServer("health-tools", log_level="WARNING")
server.tool(name="search_web", description="无需密钥地搜索公开网页，返回标题和链接，搜索结果未经医学审核。")(search_web)
server.tool(name="read_web_document", description="读取公开网页或文字版 PDF，返回原文、来源和获取时间，拒绝内网地址。")(read_web_document)
server.tool(name="cvd_risk", description="计算 Framingham 一般心血管病十年风险，适用于 30–74 岁。文字结果不能替代医生诊断。")(tools_core.framingham_cvd_risk)
server.tool(name="bmi", description="按中国成人标准计算体重指数与分级。")(tools_core.bmi)
server.tool(name="bp_category", description="按中国高血压指南给出血压水平分级。")(tools_core.bp_category)
server.tool(name="simulate_intervention", description="用风险公式估算戒烟、血压或胆固醇改变后的风险，不代表实际获益。")(tools_core.simulate_intervention)
server.tool(name="project_trajectory", description="假设其他因素不变，推演年龄增加后的风险；跳过超过 74 岁的点。")(tools_core.project_trajectory)

async def trace_tools(ctx, call_next):
    if ctx.method != "tools/call":
        return await call_next(ctx)
    parent = extract((ctx.meta or {}).get("healthpilot", {}))
    name = (ctx.params or {}).get("name", "unknown")
    allowed = {"search_web", "read_web_document", "cvd_risk", "bmi", "bp_category", "simulate_intervention", "project_trajectory"}
    with bind(**parent), span("mcp.execute", kind="server", tool=name if name in allowed else "unknown") as fields:
        result = await call_next(ctx)
        if getattr(result, "is_error", False) or (isinstance(result, dict) and result.get("isError")):
            fields["status"] = "error"
        return result


server.middleware.append(trace_tools)

if __name__ == "__main__":
    with runtime("mcp"):
        server.run(transport="stdio")
