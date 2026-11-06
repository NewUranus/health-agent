import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AccountAdmin } from "../src/admin/AccountAdmin";

const account = { id: "abc", username: "nurse", role: "user" as const, created_at: 1_700_000_000 };

describe("后台账号管理", () => {
  it("两次初始密码不一致时不创建", async () => {
    const api = vi.fn(async () => ({ accounts: [] }));
    render(<AccountAdmin api={api} />);
    await userEvent.type(screen.getByLabelText("用户名"), "nurse");
    await userEvent.type(screen.getByLabelText("初始密码"), "replacement-password-2026");
    await userEvent.type(screen.getByLabelText("再次输入初始密码"), "another-password-2026");
    await userEvent.click(screen.getByRole("button", { name: "创建账号" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("两次初始密码不一致");
    expect(api).toHaveBeenCalledTimes(1);
  });

  it("创建账号时提交角色和密码，成功提示不回显密码", async () => {
    const api = vi.fn(async (path: string) => path === "/accounts" ? { accounts: [account] } : { id: "abc", username: "nurse", role: "user" });
    render(<AccountAdmin api={api} />);
    await screen.findByText("nurse");
    await userEvent.type(screen.getByLabelText("用户名"), "nurse");
    await userEvent.selectOptions(screen.getByLabelText("角色"), "knowledge_admin");
    await userEvent.type(screen.getByLabelText("初始密码"), "replacement-password-2026");
    await userEvent.type(screen.getByLabelText("再次输入初始密码"), "replacement-password-2026");
    await userEvent.click(screen.getByRole("button", { name: "创建账号" }));
    expect(await screen.findByRole("status")).toHaveTextContent("本页不再显示");
    expect(screen.getByRole("status").textContent).not.toContain("replacement-password-2026");
    expect(api).toHaveBeenCalledWith("/accounts", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ username: "nurse", password: "replacement-password-2026", role: "knowledge_admin" }),
    }));
  });
});