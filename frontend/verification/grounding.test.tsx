import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TracePanel } from "../src/components/TracePanel";
import type { NodeData } from "../src/types";

function show(node: string, data: NodeData) {
  render(<TracePanel turns={[{ turn: 1, nodes: [{ type: "node", node, agent: "qa_agent", ms: 5, data }] }]} highlighted={null} selection={0} />);
}

describe("事实校验轨迹", () => {
  it("全部通过", () => {
    show("answer", { grounding: { checked: 4, unsupported: [], removed: 0, rewritten: false } });
    expect(screen.getByText("事实校验：4 句通过，删除 0 句")).toHaveClass("success");
  });

  it("重写后仍不支持的句子被删除", () => {
    show("answer", { grounding: { checked: 5, unsupported: ["孕期禁用"], removed: 1, rewritten: true } });
    expect(screen.getByText("事实校验：4 句通过，删除 1 句（已重写一次）")).toHaveClass("warning");
  });

  it("删光后拒答", () => {
    show("answer", { grounding: { checked: 2, unsupported: ["a", "b"], removed: 2, rewritten: true }, grounding_refused: true });
    expect(screen.getByText("删除后没有可用内容，已改为拒答。")).toBeInTheDocument();
  });

  it("建议节点要求重写", () => {
    show("verify", { ok: false, problems: ["x"], attempt: 1, grounding: { checked: 3, unsupported: ["x"], removed: 0, rewritten: false } });
    expect(screen.getByText("事实校验：1 句在资料中找不到依据，已要求重写。")).toBeInTheDocument();
  });

  it("判定模型不可用时明确提示未核对", () => {
    show("answer", { grounding_skipped: true, grounding_skip_reason: "TimeoutError" });
    expect(screen.getByText("事实校验未完成（判定模型不可用），回答未经逐句核对。")).toHaveClass("warning");
  });

  it("Mock 与模板说明原因", () => {
    show("answer", { grounding_skipped: true, grounding_skip_reason: "mock" });
    expect(screen.getByText("事实校验：Mock 模式未执行。")).toBeInTheDocument();
  });
});
