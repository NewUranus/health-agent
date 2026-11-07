import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TracePanel } from "../src/components/TracePanel";
import type { NodeData } from "../src/types";

function show(node: string, data: NodeData, agent = "orchestrator") {
  render(<TracePanel turns={[{ turn: 1, nodes: [{ type: "node", node, agent, ms: 5, data }] }]} highlighted={null} selection={0} />);
}

describe("急症双通道轨迹", () => {
  it("模型识别的急症显示原文依据", () => {
    show("safety_check", { red_flags: ["chest_pain"], source: "model", classifier: "ok", evidence: "心口像被石头压住" });
    expect(screen.getByText("模型识别出急症信号：「心口像被石头压住」，立即进入紧急指引。")).toBeInTheDocument();
  });

  it("规则命中仍按原文案提示", () => {
    show("safety_check", { red_flags: ["chest_pain"], source: "rule", classifier: "not_needed" });
    expect(screen.getByText("发现急症信号，立即进入紧急指引。")).toBeInTheDocument();
  });

  it("规则和模型都未命中时说明两路都查过", () => {
    show("safety_check", { red_flags: [], source: "none", classifier: "ok" });
    expect(screen.getByText("规则与模型均未发现急症信号，继续问诊。")).toBeInTheDocument();
  });

  it("模型分诊失败时提示只按规则判断", () => {
    show("safety_check", { red_flags: [], source: "none", classifier: "failed", classifier_failed: true });
    const note = screen.getByText("未命中急症关键词；模型分诊未完成，已按规则结果继续。");
    expect(note).toHaveClass("warning");
  });

  it("Mock 或旧事件没有模型字段时沿用原文案", () => {
    show("safety_check", { red_flags: [] });
    expect(screen.getByText("未命中急症关键词，继续问诊。")).toBeInTheDocument();
  });

  it("随访中途由模型识别急症时显示依据", () => {
    show("checkin", { red_flags: ["chest_pain"], source: "model", evidence: "胸口发紧" }, "checkin_agent");
    expect(screen.getByText(/发现急症信号，随访已中止。模型依据：「胸口发紧」/)).toBeInTheDocument();
  });
});
