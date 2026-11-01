import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HistoryPanel, RiskTrend, formatRecordDate } from "./HistoryPanel";
import type { HistoryRecord } from "../types";

const A: HistoryRecord = { at: "2026-10-30T02:00:00+00:00", kind: "assessment", risk_percent: 24.27, level: "high", sbp: 140, dbp: 90 };
const B: HistoryRecord = { at: "2026-11-30T02:00:00+00:00", kind: "checkin", risk_percent: 13.45, level: "intermediate", sbp: 130, dbp: 85, adherence: "poor", symptoms: true, bp_updated: true };
const C: HistoryRecord = { at: "2026-12-30T02:00:00+00:00", kind: "checkin", risk_percent: 11.77, level: "intermediate", sbp: 125, dbp: 80, adherence: "good", symptoms: false, bp_updated: true };

function stub(records: HistoryRecord[] | null) {
  vi.stubGlobal("fetch", vi.fn(async () => records ? new Response(JSON.stringify({ records })) : new Response("x", { status: 500 })));
}
const show = () => render(<HistoryPanel userId="u1" refreshKey="k" followup={{ status: "none" }} />);

beforeEach(() => vi.unstubAllGlobals());

describe("formatRecordDate", () => {
  it("按东八区显示日期", () => {
    expect(formatRecordDate("2026-10-31T16:30:00+00:00")).toBe("2026-11-01");
    expect(formatRecordDate("2026-10-31T15:30:00+00:00")).toBe("2026-10-31");
  });
});

describe("HistoryPanel", () => {
  it("没有记录时提示", async () => {
    stub([]);
    show();
    expect(await screen.findByText(/还没有记录/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("只有一条时不画折线，提示复查后可看趋势", async () => {
    stub([A]);
    show();
    expect(await screen.findByText(/再复查一次/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText("首次评估")).toBeInTheDocument();
  });

  it("多条时画趋势图，列表最新在前", async () => {
    stub([A, B, C]);
    const { container } = show();
    expect(await screen.findByRole("img", { name: /风险趋势/ })).toBeInTheDocument();
    const items = [...container.querySelectorAll(".history-item")];
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("2026-12-30");
    expect(items[2]).toHaveTextContent("首次评估");
  });

  it("列表显示复查详情，四舍五入到 1 位", async () => {
    stub([A, B]);
    const { container } = show();
    await screen.findByRole("img");
    const latest = container.querySelector(".history-item") as HTMLElement;
    const row = within(latest);
    expect(row.getByText("13.5%")).toBeInTheDocument();
    expect(row.getByText("中风险")).toBeInTheDocument();
    expect(row.getByText(/血压 130\/85/)).toBeInTheDocument();
    expect(row.getByText(/依从：需要改进/)).toBeInTheDocument();
    expect(row.getByText(/有不适症状/)).toBeInTheDocument();
  });

  it("首次评估不显示依从和症状", async () => {
    stub([A]);
    const { container } = show();
    await screen.findByText("首次评估");
    expect(container.querySelector(".history-item")).not.toHaveTextContent("依从");
    expect(container.querySelector(".history-item")).not.toHaveTextContent("症状");
  });

  it("接口失败时提示", async () => {
    stub(null);
    show();
    expect(await screen.findByText(/暂时无法读取/)).toBeInTheDocument();
  });
});

describe("RiskTrend", () => {
  it("每条记录一个点，只标注最新值", () => {
    const { container } = render(<RiskTrend records={[A, B, C]} />);
    expect(container.querySelectorAll(".trend-point")).toHaveLength(3);
    expect(container.querySelector(".trend-end-label")).toHaveTextContent("11.8%");
    expect(container.querySelectorAll(".trend-end-label")).toHaveLength(1);
    expect(container.querySelector(".trend-line")).toBeInTheDocument();
  });

  it("同一天的多条记录不重叠", () => {
    const { container } = render(<RiskTrend records={[A, { ...B, at: A.at }, { ...C, at: A.at }]} />);
    const xs = [...container.querySelectorAll(".trend-point")].map((p) => p.getAttribute("cx"));
    expect(new Set(xs).size).toBe(3);
  });

  it("纵轴能容纳最大值并包含分级线", () => {
    const high = { ...A, risk_percent: 93.2 };
    const { container } = render(<RiskTrend records={[high, B]} />);
    const ys = [...container.querySelectorAll(".trend-point")].map((p) => Number(p.getAttribute("cy")));
    expect(Math.min(...ys)).toBeGreaterThanOrEqual(0);
    expect(container.querySelectorAll(".trend-threshold")).toHaveLength(2);
  });

  it("悬停和键盘焦点都能看到提示", () => {
    const { container } = render(<RiskTrend records={[A, B]} />);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    const hits = container.querySelectorAll(".trend-hit");
    expect(hits).toHaveLength(2);
    fireEvent.pointerEnter(hits[0]);
    expect(screen.getByRole("tooltip")).toHaveTextContent("24.3%");
    expect(screen.getByRole("tooltip")).toHaveTextContent("2026-10-30");
    fireEvent.pointerLeave(hits[0]);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.focus(hits[1]);
    expect(screen.getByRole("tooltip")).toHaveTextContent("13.5%");
    fireEvent.blur(hits[1]);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });
});
