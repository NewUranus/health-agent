import { describe, expect, it } from "vitest";
import { progressLabel } from "./progress";

describe("progressLabel", () => {
  it.each([
    [null, undefined, "正在连接健康助手…"],
    ["safety_check", undefined, "正在整理健康档案…"],
    ["intake", undefined, "正在规划下一步…"],
    ["supervisor", { route: "assessment_agent" }, "正在计算健康风险…"],
    ["supervisor", { route: "qa_agent" }, "正在查找指南并回答，通常需要 5–15 秒…"],
    ["supervisor", { route: "simulation_agent" }, "正在模拟干预效果…"],
    ["supervisor", { route: "checkin_agent" }, "正在对比复查结果…"],
    ["supervisor", { route: "intake_agent" }, "正在整理需要补充的信息…"],
    ["risk_assess", undefined, "正在检索指南依据…"],
    ["retrieve", undefined, "正在生成干预建议，通常需要 10–20 秒…"],
    ["advise", undefined, "正在校验建议与引用…"],
    ["verify", { ok: true }, "正在制定随访计划…"],
    ["verify", { ok: false }, "引用校验未通过，正在重新生成…"],
    ["unknown_node", undefined, "正在处理…"],
  ])("%s %j", (node, data, expected) => {
    expect(progressLabel(node, data)).toBe(expected);
  });
});
