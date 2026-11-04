import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { runTask } from "../src/lib/tasks";
import { setExpectedUser } from "../src/lib/api";
import { TasksPanel } from "../src/components/TasksPanel";
import { initialSession, sessionReducer } from "../src/lib/session";

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const sse = (events: object[]) => new Response(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(""));
beforeEach(() => { setExpectedUser(null); });

it("提交响应丢失后使用相同幂等键重试，并按游标恢复中断的事件流", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  let submissions = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (url === "/api/v1/tasks") {
      if (++submissions === 1) throw new TypeError("连接丢失");
      return json({ id: "saved-task" });
    }
    if (url.endsWith("after=0")) return sse([{ type: "node", node: "intake", agent: "orchestrator", ms: 1, data: {}, seq: 1 }]);
    if (url.endsWith("after=1")) return sse([{ type: "message", content: "完成", seq: 2 }, { type: "done", seq: 3 }]);
    throw new Error("未预期地址");
  }));
  const received: object[] = [];
  await runTask({ thread_id: "thread", message: "你好", web_search: false }, "stable-key", new AbortController().signal, event => received.push(event));
  const submits = calls.filter(c => c.url === "/api/v1/tasks");
  expect(submits).toHaveLength(2);
  expect(submits.map(c => new Headers(c.init?.headers).get("Idempotency-Key"))).toEqual(["stable-key", "stable-key"]);
  expect(received).toHaveLength(3);
  expect(calls.some(c => c.url.endsWith("after=1"))).toBe(true);
});

it("幂等参数冲突不自动生成新键或重复提交", async () => {
  const fetch = vi.fn(async () => json({ detail: "同一幂等键对应不同请求" }, 409));
  vi.stubGlobal("fetch", fetch);
  await expect(runTask({ thread_id: "thread", message: "你好", web_search: false }, "stable-key", new AbortController().signal, () => {})).rejects.toThrow("同一幂等键");
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("收到完成事件后立即结束订阅，不等待服务端关闭连接", async () => {
  let cancelled = false;
  const stream = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new TextEncoder().encode('data: {"type":"done","seq":1}\n\n')); },
    cancel() { cancelled = true; },
  });
  const fetch = vi.fn(async (url: string) => url === "/api/v1/tasks" ? json({ id: "finished" }) : new Response(stream));
  vi.stubGlobal("fetch", fetch);
  await runTask({ thread_id: "thread", message: "你好", web_search: false }, "stable-key", new AbortController().signal, () => {});
  expect(cancelled).toBe(true);
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("任务列表展示保存结果并可恢复等待中的随访会话", async () => {
  const task = { id: "task-id", thread_id: "saved-thread", status: "waiting_human", request_id: "request-id", created_at: Date.now(), cancel_requested: false };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url === "/api/v1/tasks"
    ? json({ tasks: [task] }) : json({ task, events: [{ seq: 1, event: { type: "message", content: "最近有按时运动吗？" } }] })));
  const resume = vi.fn();
  render(<TasksPanel onResume={resume} busy={false} />);
  await userEvent.click(await screen.findByRole("button", { name: "查看保存结果" }));
  await screen.findByText("最近有按时运动吗？");
  await userEvent.click(screen.getByRole("button", { name: "继续此会话" }));
  expect(resume).toHaveBeenCalledWith(task, [{ type: "message", content: "最近有按时运动吗？" }]);
});

it("恢复会话同时还原引用编号和来源轨迹", () => {
  const state = sessionReducer(initialSession("account-user"), { type: "restore", threadId: "saved-thread", waiting: false, events: [
    { type: "node", node: "retrieve", agent: "qa_agent", ms: 1, data: { chunks: [{ id: "source-one", title: "来源", source: "指南", text: "正文", score: 1 }] } },
    { type: "message", content: "建议 [cite:source-one]" }, { type: "done" },
  ] });
  expect(state.threadId).toBe("saved-thread");
  expect(state.userId).toBe("account-user");
  expect(state.citations.get("1:source-one")).toBe(1);
  expect(state.traces[0].nodes[0].data?.chunks?.[0].id).toBe("source-one");
});

it("运行任务的取消操作发送受保护写请求", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return url === "/api/v1/tasks" ? json({ tasks: [{ id: "running-task", status: "running", created_at: Date.now(), cancel_requested: false }] }) : json({ status: "running", cancel_requested: true });
  }));
  render(<TasksPanel onResume={() => {}} busy={true} />);
  await userEvent.click(await screen.findByRole("button", { name: "取消任务" }));
  const cancel = calls.find(c => c.url.endsWith("/cancel"));
  expect(cancel?.init?.method).toBe("POST");
  expect(new Headers(cancel?.init?.headers).get("X-HealthPilot-Request")).toBe("1");
});
