import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { KnowledgePolicy, ModelUsage } from "../src/admin/Governance";
import { ApprovalDialog, type AdminApi, type Approval } from "../src/admin/Approvals";

it("选择指定账号生成待确认参数，切回共享时不夹带账号", async () => {
  const api = vi.fn(async () => ({ accounts: [{ id: "a".repeat(32), username: "测试甲" }, { id: "b".repeat(32), username: "测试乙" }] }));
  const submit = vi.fn(async () => {});
  render(<KnowledgePolicy api={api as AdminApi} initial={{ visibility: "shared", allowed_user_ids: [] }} busy={false} onSubmit={submit} />);
  await userEvent.selectOptions(screen.getByRole("combobox"), "restricted");
  await userEvent.click(await screen.findByRole("checkbox", { name: /测试甲/ }));
  await userEvent.click(screen.getByRole("button", { name: "生成访问范围确认单" }));
  expect(submit).toHaveBeenLastCalledWith({ visibility: "restricted", allowed_user_ids: ["a".repeat(32)] });
  await userEvent.selectOptions(screen.getByRole("combobox"), "shared");
  await userEvent.click(screen.getByRole("button", { name: "生成访问范围确认单" }));
  expect(submit).toHaveBeenLastCalledWith({ visibility: "shared", allowed_user_ids: [] });
});

it("访问范围确认展示实际账号标识，未经核对不可执行", () => {
  const approval: Approval = { id: "one", fingerprint: "f".repeat(64), action: "access", document_id: 1, status: "waiting_human", expires_at: Date.now() + 300000,
    request_id: "request", error_code: null, result: null,
    summary: { title: "测试资料", version: "v1", chunk_count: 1, risk: "影响历史引用", replaced: [], access: { visibility: "restricted", allowed_user_ids: ["a".repeat(32)] } } };
  render(<ApprovalDialog approval={approval} api={vi.fn() as AdminApi} onDone={async () => {}} onClose={() => {}} />);
  expect(screen.getByRole("heading", { name: "确认调整访问范围" })).toBeInTheDocument();
  expect(screen.getByText(/账号标识：a/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "确认并执行" })).toBeDisabled();
});

it("模型用量展示缺失统计与价格边界", async () => {
  const api = vi.fn(async () => ({ groups: [{ model: "测试模型", scene: "answer:v1", status: "succeeded", calls: 2, input_tokens: null, output_tokens: null, duration_ms: 500, unknown_usage_calls: 2 }], currency_cost: null }));
  render(<ModelUsage api={api as AdminApi} />);
  expect(await screen.findByText(/已知输入 Token 未知/)).toBeInTheDocument();
  expect(screen.getByText(/未接入价格表/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "刷新模型用量" }));
  await waitFor(() => expect(api).toHaveBeenCalledTimes(2));
});
