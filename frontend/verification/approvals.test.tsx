import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { ApprovalDialog, type Approval, type AdminApi } from "../src/admin/Approvals";
import AdminApp from "../src/admin/AdminApp";
import { TasksPanel } from "../src/components/TasksPanel";

const sample: Approval = { id: "approval-one", action: "publish", document_id: 3, fingerprint: "a".repeat(64), status: "waiting_human",
  expires_at: Date.now() + 300000, request_id: "request", error_code: null, result: null,
  summary: { title: "测试指南", version: "v2", chunk_count: 1, risk: "将替换旧版本资料。", replaced: [{ id: 2, title: "旧指南", version: "v1" }] } };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
beforeEach(() => { vi.unstubAllGlobals(); sessionStorage.clear(); });

it("核对摘要后才允许确认，提交绑定服务端内容指纹", async () => {
  const api = vi.fn(async (_path: string, _init?: RequestInit) => ({ ...sample, status: "succeeded" }));
  const done = vi.fn();
  render(<ApprovalDialog approval={sample} api={api as AdminApi} onDone={done} onClose={() => {}} />);
  expect(screen.getByRole("button", { name: "确认并执行" })).toBeDisabled();
  expect(screen.getByText(/旧指南/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.click(screen.getByRole("button", { name: "确认并执行" }));
  expect(JSON.parse(api.mock.calls[0][1]!.body as string)).toEqual({ decision: "approve", fingerprint: sample.fingerprint });
  expect(done).toHaveBeenCalledTimes(1);
});

it("过期确认禁用执行，拒绝操作无需勾选同意", async () => {
  const api = vi.fn(async (_path: string, _init?: RequestInit) => ({ ...sample, status: "rejected" }));
  const view = render(<ApprovalDialog approval={{ ...sample, expires_at: 1 }} api={api as AdminApi} onDone={async () => {}} onClose={() => {}} />);
  expect(screen.getByRole("button", { name: "确认并执行" })).toBeDisabled();
  expect(screen.getByText(/确认已过期/)).toBeInTheDocument();
  view.unmount();
  render(<ApprovalDialog approval={sample} api={api as AdminApi} onDone={async () => {}} onClose={() => {}} />);
  await userEvent.click(screen.getByRole("button", { name: "拒绝操作" }));
  expect(JSON.parse(api.mock.calls[0][1]!.body as string).decision).toBe("reject");
});

it("服务端要求确认后展示窗口，确认完成后显示发布结果", async () => {
  const document = { id: 3, title: "测试指南", source: "来源", version: "v2", status: "draft", builtin: false, chunk_count: 1, chunks: [{ id: "d3-0", text: "测试内容", page: 1, heading: "测试" }] };
  let decisionCount = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    if (url === "/api/admin/documents") return json({ documents: [document] });
    if (url === "/api/admin/documents/3") return json(document);
    if (url.endsWith("/publish")) return json({ detail: "需要确认", code: "APPROVAL_REQUIRED" }, 428);
    if (url === "/api/admin/approvals") return json(sample, 201);
    if (url.endsWith("/decision")) { decisionCount++; return json({ ...sample, status: "succeeded", result: { ...document, status: "published" } }); }
    throw new Error("未预期请求");
  }));
  render(<AdminApp />);
  await userEvent.click(await screen.findByRole("button", { name: "测试指南" }));
  await userEvent.click(screen.getByRole("button", { name: "发布", exact: true }));
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
  expect(decisionCount).toBe(0);
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.click(screen.getByRole("button", { name: "确认并执行" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByText("已发布")).toBeInTheDocument();
  expect(decisionCount).toBe(1);
});

it("失败任务标记人工协助只保存处理记录，不重复提交任务", async () => {
  const calls: { url: string; init: RequestInit }[] = [];
  const task = { id: "failed-one", status: "interrupted", created_at: Date.now(), health_committed: true };
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    if (url.endsWith("/review")) return json({ ...task, review_disposition: "needs_support" });
    return json({ tasks: [task] });
  }));
  render(<TasksPanel onResume={() => {}} busy={false} />);
  expect(await screen.findByText(/健康记录已保存/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "标记需要人工协助" }));
  const call = calls.find(row => row.url.endsWith("/review"));
  expect(JSON.parse(call!.init.body as string)).toEqual({ disposition: "needs_support" });
  expect(calls.filter(row => row.init.method === "POST")).toHaveLength(1);
  await userEvent.click(screen.getByRole("checkbox"));
  await waitFor(() => expect(calls.some(row => row.url.endsWith("?failed=true"))).toBe(true));
});
