const routes: Record<string, string> = {
  assessment_agent: "正在计算健康风险…",
  qa_agent: "正在查找指南并回答，通常需要 5–15 秒…",
  simulation_agent: "正在模拟干预效果…",
  checkin_agent: "正在对比复查结果…",
  intake_agent: "正在整理需要补充的信息…",
};

const labels: Record<string, string> = {
  safety_check: "正在整理健康档案…",
  intake: "正在规划下一步…",
  risk_assess: "正在检索指南依据…",
  retrieve: "正在生成干预建议，通常需要 10–20 秒…",
  advise: "正在校验建议与引用…",
};

export function progressLabel(node: string | null, data?: Record<string, unknown>): string {
  if (node === null) return "正在连接健康助手…";
  if (node === "supervisor") return routes[String(data?.route)] ?? "正在处理…";
  if (node === "verify") {
    return data?.ok === false ? "引用校验未通过，正在重新生成…" : "正在制定随访计划…";
  }
  return labels[node] ?? "正在处理…";
}
