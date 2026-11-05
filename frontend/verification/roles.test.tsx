import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AdminApp from "../src/admin/AdminApp";
import { AuthContext, type UserRole } from "../src/auth/AuthGate";
import { setExpectedUser } from "../src/lib/api";

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  sessionStorage.clear();
  setExpectedUser(null);
});

function renderAdmin(role: UserRole | null) {
  const user = role ? { id: "admin-1", username: "admin", role } : null;
  return render(
    <AuthContext.Provider value={{ user, logout: () => {}, busy: false, required: true }}>
      <AdminApp />
    </AuthContext.Provider>,
  );
}

describe("后台角色页签", () => {
  it("知识管理员只看到知识页签", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("/admin/documents")) return json({ documents: [] });
      throw new Error(url);
    }));
    renderAdmin("knowledge_admin");
    await screen.findByRole("heading", { name: "知识库后台" });
    expect(screen.getByText("知识管理员")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "文档" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "确认记录" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "运行状态" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "日志检索" })).not.toBeInTheDocument();
  });

  it("运维管理员只看到运维页签", async () => {
    const overview = {
      release: "test", tasks: {}, workers: [], oldest_pending_seconds: 0, stale_tasks: 0,
      completed_24h: 0, average_completion_seconds_24h: 0, needs_support: 0, admission_paused: false,
      costs_24h: { cost_microusd: null, priced_calls: 0 }, alerts: [], failures_24h: [],
    };
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("/admin/operations")) return json(overview);
      if (url.includes("/admin/queue")) return json({ tasks: [] });
      if (url.includes("/admin/support")) return json({ tasks: [] });
      if (url.includes("/admin/audit")) return json({ records: [] });
      throw new Error(url);
    }));
    renderAdmin("ops_admin");
    await screen.findByRole("heading", { name: "运维后台" });
    expect(screen.getByRole("tab", { name: "运行状态" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "日志检索" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "文档" })).not.toBeInTheDocument();
  });

  it("无角色会话时仍显示共享口令登录", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "密码错误" }, 401)));
    renderAdmin(null);
    await waitFor(() => expect(screen.getByRole("heading", { name: "管理员登录" })).toBeInTheDocument());
    expect(screen.getByLabelText("密码")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "使用角色账号登录" })).toHaveAttribute("href", "/login");
  });
});
