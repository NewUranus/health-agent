import { screen, render, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App";
import { AuthGate } from "../src/auth/AuthGate";
import { setExpectedUser } from "../src/lib/api";

const json = (value: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json", ...headers } });
const guestUser = { id: "guest-account", username: "guest-abc123", role: "guest" };
const guestSession = (remaining = 20) => ({ required: true, user: guestUser, expires_at: Math.floor(Date.now() / 1000) + 3600,
  guest: { limit: 20, remaining, hours: 24, daily_available: true } });

beforeEach(() => { localStorage.clear(); setExpectedUser(null); window.history.replaceState(null, "", "/"); });

function server({ offered = true, guest = () => json(guestSession(), 201), me }: {
  offered?: boolean; guest?: () => Response; me?: () => Response } = {}) {
  const calls: string[] = [];
  let remaining = 20;
  let started = false;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    calls.push(url);
    if (url.endsWith("/auth/me")) {
      if (me) return me();
      return started ? json(guestSession(remaining)) : json({ detail: "请先登录。" }, 401, offered ? { "X-HealthPilot-Guest": "available" } : {});
    }
    if (url.endsWith("/auth/guest")) { started = true; return guest(); }
    if (url === "/api/health") return json({ ok: true, mock: false });
    if (url.startsWith("/api/followup")) return json({ status: "none" });
    if (url === "/api/v1/tasks") { remaining -= 1; return json({ id: "task-guest" }); }
    if (url.startsWith("/api/v1/tasks/task-guest/events")) return new Response('data: {"type":"message","content":"已收到"}\n\ndata: {"type":"done"}\n\n');
    throw new Error("未预期请求：" + url);
  }));
  return calls;
}

describe("免登录体验", () => {
  it("首页未登录直接进入游客模式，高级功能提示登录", async () => {
    const calls = server();
    render(<AuthGate><App /></AuthGate>);
    expect(await screen.findByText("免登录体验 · 剩余 20/20 条")).toBeInTheDocument();
    expect(calls.slice(0, 2)).toEqual(["/api/v1/auth/me", "/api/v1/auth/guest"]);
    expect(screen.getByRole("checkbox", { name: /联网搜索.*登录后可用/ })).toBeDisabled();
    expect(screen.queryByRole("tab", { name: "我的会话" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "任务记录" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "退出登录" })).not.toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("登录后可使用联网搜索、我的会话、任务记录和导出");
  });

  it("发送后刷新剩余额度", async () => {
    server();
    render(<AuthGate><App /></AuthGate>);
    await screen.findByText("免登录体验 · 剩余 20/20 条");
    await userEvent.type(screen.getByLabelText("输入健康信息或问题"), "你好{Enter}");
    await screen.findByText("已收到");
    expect(await screen.findByText("免登录体验 · 剩余 19/20 条")).toBeInTheDocument();
  });

  it("点登录显示登录表单，可返回继续体验", async () => {
    server();
    render(<AuthGate><App /></AuthGate>);
    await screen.findByText("免登录体验 · 剩余 20/20 条");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByLabelText("账号")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/login");
    await userEvent.click(screen.getByRole("button", { name: "继续免登录体验" }));
    expect(await screen.findByText("免登录体验 · 剩余 20/20 条")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
  });

  it("服务端未开放时只显示登录页，不尝试创建游客", async () => {
    const calls = server({ offered: false });
    render(<AuthGate><App /></AuthGate>);
    await screen.findByLabelText("账号");
    expect(calls).toEqual(["/api/v1/auth/me"]);
    expect(screen.queryByRole("button", { name: "免登录体验" })).not.toBeInTheDocument();
  });

  it("开启失败时展示原因并停在登录页", async () => {
    const calls = server({ guest: () => json({ detail: "当前网络今天开启免登录体验的次数已达上限，请登录后使用。", code: "GUEST_LIMITED" }, 429) });
    render(<AuthGate><App /></AuthGate>);
    expect(await screen.findByRole("alert")).toHaveTextContent("次数已达上限");
    expect(screen.getByLabelText("账号")).toBeInTheDocument();
    await waitFor(() => expect(calls.filter(url => url.endsWith("/auth/guest"))).toHaveLength(1));
  });

  it("直接打开登录页时显示登录表单和体验入口", async () => {
    window.history.replaceState(null, "", "/login");
    const calls = server();
    render(<AuthGate><App /></AuthGate>);
    await screen.findByLabelText("账号");
    expect(calls).toEqual(["/api/v1/auth/me"]);
    await userEvent.click(screen.getByRole("button", { name: "免登录体验" }));
    expect(await screen.findByText("免登录体验 · 剩余 20/20 条")).toBeInTheDocument();
  });

  it("已是游客时打开登录页，保留返回入口", async () => {
    window.history.replaceState(null, "", "/login");
    server({ me: () => json(guestSession(5)) });
    render(<AuthGate><App /></AuthGate>);
    await screen.findByLabelText("账号");
    await userEvent.click(screen.getByRole("button", { name: "继续免登录体验" }));
    expect(await screen.findByText("免登录体验 · 剩余 5/20 条")).toBeInTheDocument();
  });
});

describe("运维面板的免登录体验", () => {
  it("显示游客用量并可暂停", async () => {
    const { Operations } = await import("../src/admin/Operations");
    const overview = { release: "t", tasks: {}, workers: [], oldest_pending_seconds: 0, stale_tasks: 0, completed_24h: 0,
      average_completion_seconds_24h: 0, needs_support: 0, admission_paused: false, costs_24h: { cost_microusd: null, priced_calls: 0 },
      alerts: [], failures_24h: [], guest: { enabled: true, paused: false, active: 3, messages_today: 42, daily_limit: 200, per_guest_limit: 20, hours: 24 } };
    const api = vi.fn(async (path: string) => path === "/operations" ? overview : path === "/audit" || path.startsWith("/audit") ? { records: [] } : { tasks: [] });
    vi.stubGlobal("confirm", () => true);
    render(<Operations api={api as never} />);
    expect(await screen.findByText("42 / 200")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "暂停免登录体验" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("/guest", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ paused: true }) })));
  });
});
