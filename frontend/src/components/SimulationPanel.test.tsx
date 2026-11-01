import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SimulationPanel } from "./SimulationPanel";

const PROFILE = { sex: "male", age: 55, total_chol: 5.5, hdl: 1.3, sbp: 140, bp_treated: false, smoker: true, diabetes: false };
const RESULT = { baseline: 24.27, simulated: 13.45, absolute_reduction: 10.82, relative_reduction_percent: 44.6, changes: ["戒烟"], note: "这是基于风险公式的估算" };

function stub(respond: (body: Record<string, unknown>) => Response | Promise<Response> = () => new Response(JSON.stringify(RESULT))) {
  const bodies: Record<string, unknown>[] = [];
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body));
    bodies.push(body);
    return respond(body);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { bodies, fetchMock };
}

beforeEach(() => vi.unstubAllGlobals());

describe("SimulationPanel", () => {
  it("档案不全时提示先评估，不发请求", () => {
    const { fetchMock } = stub();
    render(<SimulationPanel profile={{ age: 55 }} />);
    expect(screen.getByText(/先完成一次风险评估/)).toBeInTheDocument();
    expect(screen.queryByRole("slider")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("年龄超出模型范围时提示，不发请求", () => {
    const { fetchMock } = stub();
    render(<SimulationPanel profile={{ ...PROFILE, age: 80 }} />);
    expect(screen.getByText(/30–74 岁/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("滑块上限是当前值，初始请求不带任何改变，字段名对应接口", async () => {
    const { bodies, fetchMock } = stub();
    render(<SimulationPanel profile={PROFILE} />);
    expect(screen.getByRole("slider", { name: "目标收缩压" })).toHaveAttribute("max", "140");
    expect(screen.getByRole("slider", { name: "目标总胆固醇" })).toHaveAttribute("max", "5.5");
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/tools/simulate_intervention");
    expect(bodies[0]).toEqual({ sex: "male", age: 55, total_chol_mmol: 5.5, hdl_mmol: 1.3, sbp: 140, bp_treated: false, smoker: true, diabetes: false });
  });

  it("显示结果，数值四舍五入到 1 位", async () => {
    stub();
    render(<SimulationPanel profile={PROFILE} />);
    expect(await screen.findByText("24.3%")).toBeInTheDocument();
    expect(screen.getByText("13.5%")).toBeInTheDocument();
    expect(screen.getByText(/绝对下降 10.8 个百分点/)).toBeInTheDocument();
    expect(screen.getByText(/基于风险公式的估算/)).toBeInTheDocument();
  });

  it("调整后发送目标值", async () => {
    const { bodies } = stub();
    render(<SimulationPanel profile={PROFILE} />);
    await waitFor(() => expect(bodies).toHaveLength(1));
    fireEvent.change(screen.getByRole("slider", { name: "目标收缩压" }), { target: { value: "120" } });
    fireEvent.change(screen.getByRole("slider", { name: "目标总胆固醇" }), { target: { value: "4.5" } });
    await userEvent.click(screen.getByRole("checkbox", { name: /戒烟/ }));
    await waitFor(() => expect(bodies.at(-1)).toMatchObject({ sbp_target: 120, total_chol_target: 4.5, quit_smoking: true }));
    expect(screen.getByText(/120 mmHg/)).toBeInTheDocument();
    expect(screen.getByText(/4\.5 mmol\/L/)).toBeInTheDocument();
  });

  it("连续拖动只发最后一次", async () => {
    const { bodies } = stub();
    render(<SimulationPanel profile={PROFILE} />);
    await waitFor(() => expect(bodies).toHaveLength(1));
    const slider = screen.getByRole("slider", { name: "目标收缩压" });
    for (const value of ["135", "130", "125", "120"]) fireEvent.change(slider, { target: { value } });
    await waitFor(() => expect(bodies.at(-1)).toMatchObject({ sbp_target: 120 }));
    expect(bodies).toHaveLength(2);
  });

  it("旧请求晚到时不覆盖新结果", async () => {
    let releaseOld!: () => void;
    const old = new Promise<void>((resolve) => { releaseOld = resolve; });
    stub(async (body) => {
      if (body.sbp_target === 130) {
        await old;
        return new Response(JSON.stringify({ ...RESULT, simulated: 99 }));
      }
      return new Response(JSON.stringify({ ...RESULT, simulated: body.sbp_target === 120 ? 11 : 24.27, changes: [] }));
    });
    render(<SimulationPanel profile={PROFILE} />);
    const slider = screen.getByRole("slider", { name: "目标收缩压" });
    await screen.findAllByText("24.3%");
    fireEvent.change(slider, { target: { value: "130" } });
    await new Promise((resolve) => setTimeout(resolve, 300));
    fireEvent.change(slider, { target: { value: "120" } });
    expect(await screen.findByText("11.0%")).toBeInTheDocument();
    releaseOld();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByText("99.0%")).not.toBeInTheDocument();
    expect(screen.getByText("11.0%")).toBeInTheDocument();
  });

  it("不吸烟时不显示戒烟选项", async () => {
    stub();
    render(<SimulationPanel profile={{ ...PROFILE, smoker: false }} />);
    await screen.findByText("24.3%");
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("接口失败时显示提示", async () => {
    stub(() => new Response("x", { status: 503 }));
    render(<SimulationPanel profile={PROFILE} />);
    expect(await screen.findByText(/模拟暂不可用/)).toBeInTheDocument();
  });

  it("档案变化后滑块回到新的当前值", async () => {
    stub();
    const view = render(<SimulationPanel profile={PROFILE} />);
    fireEvent.change(screen.getByRole("slider", { name: "目标收缩压" }), { target: { value: "120" } });
    view.rerender(<SimulationPanel profile={{ ...PROFILE, sbp: 130 }} />);
    const slider = screen.getByRole("slider", { name: "目标收缩压" });
    expect(slider).toHaveAttribute("max", "130");
    expect(slider).toHaveValue("130");
  });

  it("说明不会修改档案", async () => {
    stub();
    render(<SimulationPanel profile={PROFILE} />);
    expect(screen.getByText(/不会修改/)).toBeInTheDocument();
    await screen.findByText("24.3%");
  });
});
