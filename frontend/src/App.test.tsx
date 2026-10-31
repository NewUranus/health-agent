import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const EXAMPLE = "男，55岁，血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药";

const node = (name: string, agent: string, data: object = {}, ms = 5) => ({ type: "node", node: name, agent, ms, data });
const cvd = { risk_percent: 13.45, level: "intermediate", model: "m", note: "n" };
const FULL_RUN = [
  node("safety_check", "orchestrator", { red_flags: [] }),
  node("intake", "orchestrator", { profile: { sex: "male", age: 55, smoker: false }, missing: [], from_memory: ["age"] }),
  node("supervisor", "orchestrator", { route: "assessment_agent", reason: "资料完整" }),
  node("risk_assess", "assessment_agent", {
    tool_calls: [{ tool: "cvd_risk", args: { age: 55 }, result: cvd, via: "mcp", cached: false }],
  }),
  node("retrieve", "intervention_agent", {
    chunks: [{ id: "salt_reduction", title: "减少钠盐摄入", source: "《指南》", score: 0.5 }],
  }),
  node("advise", "intervention_agent", { prompt_version: "2.0" }),
  node("verify", "intervention_agent", { ok: true, problems: [], attempt: 1, fallback: false }),
  node("followup", "followup_agent", { followup: "建议3个月复查" }),
  { type: "message", content: "## 评估结果\n- 限盐 [cite:salt_reduction]" },
  { type: "done" },
];

const sse = (events: object[]) => events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");

function streamResponse(text: string) {
  return new Response(
    new ReadableStream({
      start(c) {
        c.enqueue(new TextEncoder().encode(text));
        c.close();
      },
    }),
    { headers: { "content-type": "text/event-stream" } },
  );
}

type ChatHandler = (body: Record<string, string>) => Response | Promise<Response>;

function mockServer(chat: ChatHandler = () => streamResponse(sse(FULL_RUN)), health: object | null = { ok: true, mock: true }) {
  const bodies: Record<string, string>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/health") {
        if (!health) throw new Error("down");
        return new Response(JSON.stringify(health), { headers: { "content-type": "application/json" } });
      }
      const body = JSON.parse(String(init?.body));
      bodies.push(body);
      return chat(body);
    }),
  );
  return bodies;
}

const input = () => screen.getByRole("textbox");
const send = (name = /开始一次风险评估/) => userEvent.click(screen.getByRole("button", { name }));
const finished = () => waitFor(() => expect(screen.getByRole("button", { name: /发送/ })).toBeEnabled());

beforeEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("显示欢迎语和运行模式", async () => {
    mockServer();
    render(<App />);
    expect(screen.getByText(/你好，我是 HealthPilot/)).toBeInTheDocument();
    expect(await screen.findByText(/Mock 演示模式/)).toBeInTheDocument();
  });

  it("真实模式和服务不可用", async () => {
    mockServer(undefined, { ok: true, mock: false });
    const first = render(<App />);
    expect(await screen.findByText(/千问真实模式/)).toBeInTheDocument();
    first.unmount();
    mockServer(undefined, null);
    render(<App />);
    expect(await screen.findByText(/服务暂不可用/)).toBeInTheDocument();
  });

  it("示例按钮发送预设内容和会话标识", async () => {
    const bodies = mockServer();
    render(<App />);
    await send();
    await finished();
    expect(bodies).toHaveLength(1);
    expect(bodies[0].message).toBe(EXAMPLE);
    expect(bodies[0].thread_id).toBeTruthy();
    expect(bodies[0].user_id).toBe(localStorage.getItem("healthpilot-user"));
  });

  it("完整一轮：轨迹、档案、风险和回复都更新", async () => {
    mockServer();
    const { container } = render(<App />);
    await send();
    await finished();
    expect(container.querySelectorAll(".trace-card")).toHaveLength(8);
    expect(screen.getByText("第 1 轮对话")).toBeInTheDocument();
    expect(screen.getByText("计算健康风险")).toBeInTheDocument();
    expect(screen.getAllByText(/评估 Agent/).length).toBeGreaterThan(0);
    expect(screen.getByText(/cvd_risk/)).toBeInTheDocument();
    expect(screen.getByText("减少钠盐摄入")).toBeInTheDocument();
    expect(screen.getByText(/引用校验通过/)).toBeInTheDocument();
    expect(screen.getAllByText(/来自长期记忆/).length).toBeGreaterThan(0);
    expect(screen.getByText("13.5%")).toBeInTheDocument();
    expect(screen.getByText("中风险")).toBeInTheDocument();
    expect(screen.getByText("55")).toBeInTheDocument();
    expect(screen.getByText("评估结果")).toBeInTheDocument();
  });

  it("处理中显示进度并禁用按钮，收到事件后进度跟着变", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({ start: (c) => void (controller = c) });
    mockServer(() => new Response(stream));
    render(<App />);
    await send();
    const status = screen.getByRole("status");
    await waitFor(() => expect(status).toHaveTextContent("正在连接健康助手…"));
    expect(screen.getByRole("button", { name: /处理中/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /开始一次风险评估/ })).toBeDisabled();

    controller.enqueue(new TextEncoder().encode(sse([node("retrieve", "intervention_agent", { chunks: [] })])));
    await waitFor(() => expect(status).toHaveTextContent("正在生成干预建议，通常需要 10–20 秒…"));

    controller.enqueue(new TextEncoder().encode(sse([{ type: "message", content: "好" }, { type: "done" }])));
    controller.close();
    await finished();
    expect(status).not.toHaveTextContent("正在生成干预建议");
  });

  it("点击引用高亮对应来源，轨迹收起时自动展开", async () => {
    mockServer();
    const { container } = render(<App />);
    await send();
    await finished();
    await userEvent.click(screen.getByRole("button", { name: /收起轨迹/ }));
    expect(screen.queryByText("减少钠盐摄入")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /展开轨迹/ })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "查看引用来源 salt_reduction" }));
    const source = container.querySelector(".source-card");
    expect(source).toHaveTextContent("减少钠盐摄入");
    expect(source).toHaveClass("highlight");
  });

  it("Enter 发送，Shift+Enter 换行，输入法组合中不发送", async () => {
    const bodies = mockServer();
    render(<App />);
    await userEvent.type(input(), "第一行{Shift>}{Enter}{/Shift}第二行");
    expect(bodies).toHaveLength(0);
    fireEvent.keyDown(input(), { key: "Enter", isComposing: true });
    expect(bodies).toHaveLength(0);
    await userEvent.type(input(), "{Enter}");
    await finished();
    expect(bodies[0].message).toBe("第一行\n第二行");
    expect(input()).toHaveValue("");
  });

  it("空白内容不发送", async () => {
    const bodies = mockServer();
    render(<App />);
    await userEvent.type(input(), "   {Enter}");
    expect(bodies).toHaveLength(0);
  });

  it.each([
    ["error 事件", () => streamResponse(sse([{ type: "error", message: "本轮未能完成" }, { type: "done" }])), /本轮未能完成/],
    ["流提前结束", () => streamResponse(sse([node("safety_check", "orchestrator", { red_flags: [] })])), /连接中断/],
    ["HTTP 错误", () => new Response("x", { status: 500 }), /连接暂不可用/],
    [
      "网络异常",
      () => {
        throw new TypeError("Failed to fetch");
      },
      /./,
    ],
  ])("%s：显示错误气泡，按钮恢复", async (_name, chat, text) => {
    mockServer(chat);
    const { container } = render(<App />);
    await send();
    await finished();
    const bubble = container.querySelector(".message.error");
    expect(bubble).not.toBeNull();
    expect(within(bubble as HTMLElement).getByText(text)).toBeInTheDocument();
  });

  it("新会话换 thread_id、保留 user_id 并清空；引用编号重新开始", async () => {
    const bodies = mockServer();
    const { container } = render(<App />);
    await send();
    await finished();
    await userEvent.click(screen.getByRole("button", { name: /新会话/ }));
    expect(container.querySelectorAll(".trace-card")).toHaveLength(0);
    expect(screen.queryByText("13.5%")).not.toBeInTheDocument();
    expect(screen.queryByText("评估结果")).not.toBeInTheDocument();
    expect(screen.getByText(/你好，我是 HealthPilot/)).toBeInTheDocument();

    await send();
    await finished();
    expect(bodies[1].thread_id).not.toBe(bodies[0].thread_id);
    expect(bodies[1].user_id).toBe(bodies[0].user_id);
    expect(screen.getByRole("button", { name: "查看引用来源 salt_reduction" })).toHaveTextContent("1");
    expect(screen.getByText("第 1 轮对话")).toBeInTheDocument();
  });

  it("清除身份同时换 user_id", async () => {
    const bodies = mockServer();
    render(<App />);
    await send();
    await finished();
    await userEvent.click(screen.getByRole("button", { name: /清除身份/ }));
    await send();
    await finished();
    expect(bodies[1].user_id).not.toBe(bodies[0].user_id);
    expect(bodies[1].user_id).toBe(localStorage.getItem("healthpilot-user"));
  });

  it("刷新后 user_id 不变", async () => {
    const bodies = mockServer();
    const first = render(<App />);
    await send();
    await finished();
    first.unmount();
    render(<App />);
    await send();
    await finished();
    expect(bodies[1].user_id).toBe(bodies[0].user_id);
  });

  it("localStorage 不可用时照常工作", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const bodies = mockServer();
    render(<App />);
    await send();
    await finished();
    expect(bodies[0].user_id).toBeTruthy();
  });

  it("模型降级和校验回退有提示", async () => {
    mockServer(() =>
      streamResponse(
        sse([
          node("advise", "intervention_agent", { degraded: true }),
          node("verify", "intervention_agent", { ok: true, attempt: 3, fallback: true, problems: [] }),
          { type: "message", content: "好" },
          { type: "done" },
        ]),
      ),
    );
    render(<App />);
    await send();
    await finished();
    expect(screen.getByText(/模型暂不可用/)).toBeInTheDocument();
    expect(screen.getByText(/已使用安全模板/)).toBeInTheDocument();
  });

  it("源码不使用 dangerouslySetInnerHTML", () => {
    const walk = (dir: string): string[] =>
      readdirSync(dir).flatMap((f) => {
        const path = join(dir, f);
        return statSync(path).isDirectory() ? walk(path) : [path];
      });
    const sources = walk(join(process.cwd(), "src")).filter((f) => /\.tsx?$/.test(f) && !/\.test\./.test(f));
    expect(sources.length).toBeGreaterThan(0);
    for (const file of sources) expect(readFileSync(file, "utf8")).not.toContain("dangerouslySetInnerHTML");
  });
});
