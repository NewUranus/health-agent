import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { PasswordChange } from "../src/auth/PasswordChange";

describe("修改密码", () => {
  it("两次新密码不一致时不提交", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<PasswordChange />);
    await userEvent.click(screen.getByRole("button", { name: "修改密码" }));
    await userEvent.type(screen.getByLabelText("当前密码"), "current-password-2026");
    await userEvent.type(screen.getByLabelText("新密码"), "replacement-password-2026");
    await userEvent.type(screen.getByLabelText("再次输入新密码"), "another-password-2026");
    await userEvent.click(screen.getByRole("button", { name: "保存新密码" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("两次新密码不一致");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("提交当前密码和新密码，不把密码留在成功提示里", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<PasswordChange />);
    await userEvent.click(screen.getByRole("button", { name: "修改密码" }));
    await userEvent.type(screen.getByLabelText("当前密码"), "current-password-2026");
    await userEvent.type(screen.getByLabelText("新密码"), "replacement-password-2026");
    await userEvent.type(screen.getByLabelText("再次输入新密码"), "replacement-password-2026");
    await userEvent.click(screen.getByRole("button", { name: "保存新密码" }));
    expect(await screen.findByRole("status")).toHaveTextContent("其他设备上的登录已退出");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      current_password: "current-password-2026",
      new_password: "replacement-password-2026",
    });
    expect(screen.getByRole("status").textContent).not.toContain("replacement-password-2026");
  });
});
