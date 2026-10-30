"""通过标准输入输出暴露五个确定性健康工具。"""
from mcp.server.mcpserver import MCPServer
from app import tools_core

server = MCPServer("health-tools", log_level="WARNING")
server.tool(name="cvd_risk", description="计算 Framingham 一般心血管病十年风险，适用于 30–74 岁。文字结果不能替代医生诊断。")(tools_core.framingham_cvd_risk)
server.tool(name="bmi", description="按中国成人标准计算体重指数与分级。")(tools_core.bmi)
server.tool(name="bp_category", description="按中国高血压指南给出血压水平分级。")(tools_core.bp_category)
server.tool(name="simulate_intervention", description="用风险公式估算戒烟、血压或胆固醇改变后的风险，不代表实际获益。")(tools_core.simulate_intervention)
server.tool(name="project_trajectory", description="假设其他因素不变，推演年龄增加后的风险；跳过超过 74 岁的点。")(tools_core.project_trajectory)

if __name__ == "__main__":
    server.run(transport="stdio")
