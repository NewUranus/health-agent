import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App";
import { AuthGate } from "../src/auth/AuthGate";
import { apiFetch, setExpectedUser } from "../src/lib/api";

const user = { id: "account-alice", username: "alice" };
const session = () => ({ required: true, user, expires_at: Math.floor(Date.now() / 1000) + 3600 });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => { localStorage.clear(); setExpectedUser(null); });

function server(me: () => Response = () => json({ detail: "请先登录。" }, 401)) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (url.endsWith("/auth/me")) return me();
    if (url.endsWith("/auth/login")) return json(session());
    if (url.endsWith("/auth/logout")) return json({ ok: true });
    if (url === "/api/health") return json({ ok: true, mock: true });
    if (url.startsWith("/api/followup")) return json({ status: "none" });
    if (url === "/api/v1/tasks") return json({ id: "task-test" });
    if (url.startsWith("/api/v1/tasks/task-test/events")) return new Response('data: {"type":"message","content":"已收到"}\n\ndata: {"type":"done"}\n\n');
    throw new Error("未预期请求");
  }));
  return calls;
}

async function enter() {
  await userEvent.type(await screen.findByLabelText("账号"), "alice");
  await userEvent.type(screen.getByLabelText("密码"), "validation-password-2026");
  await userEvent.click(screen.getByRole("button", { name: "登录" }));
}

describe("登录与页面隔离", () => {
  it("未登录不加载健康数据，登录后用服务端身份，退出清除页面", async () => {
    const calls = server();
    localStorage.setItem("healthpilot-user", "forged-local-id");
    render(<AuthGate><App /></AuthGate>);
    await screen.findByLabelText("账号");
    expect(calls.map(c => c.url)).toEqual(["/api/v1/auth/me"]);
    expect(screen.queryByLabelText("健康对话")).not.toBeInTheDocument();
    await enter();
    await screen.findByRole("button", { name: "退出登录" });
    expect(screen.queryByRole("button", { name: "清除身份" })).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("输入健康信息或问题"), "你好{Enter}");
    await screen.findByText("已收到");
    const chat = calls.find(c => c.url === "/api/v1/tasks")!;
    expect(JSON.parse(String(chat.init?.body)).user_id).toBeUndefined();
    expect(new Headers(chat.init?.headers).get("Idempotency-Key")).toBeTruthy();
    expect(new Headers(chat.init?.headers).get("X-HealthPilot-User")).toBe(user.id);
    expect(new Headers(chat.init?.headers).get("X-HealthPilot-Request")).toBe("1");
    expect(JSON.stringify(localStorage)).not.toContain("validation-password");
    await userEvent.click(screen.getByRole("button", { name: "退出登录" }));
    await screen.findByLabelText("账号");
    expect(screen.queryByText("已收到")).not.toBeInTheDocument();
  });

  it("刷新页面从服务器恢复登录，无需浏览器身份 ID", async () => {
    server(() => json(session()));
    render(<AuthGate><App /></AuthGate>);
    await screen.findByText("alice");
    expect(screen.queryByLabelText("密码")).not.toBeInTheDocument();
    expect(localStorage.getItem("healthpilot-user")).toBeNull();
  });

  it("登录状态请求失败时拒绝进入聊天，并提供重试", async () => {
    server(() => json({}, 503));
    render(<AuthGate><App /></AuthGate>);
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法确认登录状态");
    expect(screen.queryByLabelText("健康对话")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新连接" })).toBeEnabled();
  });

  it("跨标签账号变化清空旧会话", async () => {
    server(() => json(session()));
    render(<AuthGate><App /></AuthGate>);
    await screen.findByText("alice");
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: "healthpilot-auth-change", newValue: "another-login" })));
    await screen.findByLabelText("账号");
    expect(screen.queryByLabelText("健康对话")).not.toBeInTheDocument();
  });

  it("API 返回会话失效后卸载旧页面", async () => {
    server(() => json(session()));
    render(<AuthGate><App /></AuthGate>);
    await screen.findByText("alice");
    vi.stubGlobal("fetch", vi.fn(async () => json({ code: "SESSION_CHANGED" }, 403)));
    await act(async () => { await apiFetch("/api/history"); });
    await screen.findByLabelText("账号");
    expect(screen.queryByText("alice")).not.toBeInTheDocument();
  });

  it("登录失败展示服务端提示，未进入聊天", async () => {
    server();
    render(<AuthGate><App /></AuthGate>);
    await screen.findByLabelText("账号");
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "账号或密码错误。" }, 401)));
    await enter();
    expect(await screen.findByRole("alert")).toHaveTextContent("账号或密码错误");
    expect(screen.queryByLabelText("健康对话")).not.toBeInTheDocument();
  });

  it("演示模式明确显示免登录，不把服务失败当作演示模式", async () => {
    server(() => json({ required: false, user: null }));
    render(<AuthGate><App /></AuthGate>);
    await screen.findByText(/免登录演示/);
    expect(screen.getByRole("button", { name: "清除身份" })).toBeEnabled();
  });

  it("退出网络失败不会虚报成功", async () => {
    server(() => json(session()));
    render(<AuthGate><App /></AuthGate>);
    await screen.findByText("alice");
    vi.stubGlobal("fetch", vi.fn(async () => json({}, 503)));
    fireEvent.click(screen.getByRole("button", { name: "退出登录" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("退出未完成"));
    expect(screen.getByRole("button", { name: "退出登录" })).toBeEnabled();
  });
});
