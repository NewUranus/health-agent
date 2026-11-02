import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AdminApp from "./AdminApp";

const draftChunk = { id: "d3-0", seq: 0, page: 12, heading: "第一章", text: "收缩压140" };
const draft = { id: 3, title: "指南", source: "《指南》", version: "2020", filename: "a.pdf", status: "draft", builtin: false, chunk_count: 1, created_at: "2026-11-02T00:00:00Z", published_at: null };
const builtin = { ...draft, id: 1, title: "内置资料", status: "published", builtin: true, chunk_count: 16 };
const stats = { backend: "pgvector", embedding_model: "qwen3.7-text-embedding", dimension: 1024, documents: 2, published_documents: 1, draft_documents: 1, chunks: 17, vectors: 16, min_similarity: 0.44 };
const row = { id: "salt", document_id: 1, document_title: "内置资料", source: "《指南》", page: 7, heading: "限盐", text: "每天吃盐不超过5克", dimension: 1024, vector_preview: [0.0123, -0.0456] };

type Handler = (url: string, init: RequestInit) => Response | Promise<Response> | undefined;
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

function server(custom: Handler = () => undefined) {
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url, init });
      const special = await custom(url, init);
      if (special) return special;
      if (url.startsWith("/api/admin/stats")) return json(stats);
      if (url.startsWith("/api/admin/chunks")) return json({ total: 120, chunks: [row] });
      if (url.startsWith("/api/admin/search")) return json({ results: [{ ...row, score: 0.71 }], min_similarity: 0.44 });
      if (url === "/api/admin/documents" && init.method === "POST") return json({ ...draft, chunks: [draftChunk] }, 201);
      if (url === "/api/admin/documents") return json({ documents: [builtin] });
      if (url === "/api/admin/documents/3/publish") return json({ ...draft, status: "published" });
      if (url === "/api/admin/documents/3") return json({ ...draft, chunks: [draftChunk] });
      if (url.includes("/chunks/")) return json({ ok: true });
      return json({ detail: "未处理：" + url }, 500);
    }),
  );
  return calls;
}

async function login(password = "secret") {
  await userEvent.type(screen.getByLabelText(/密码/), password);
  await userEvent.click(screen.getByRole("button", { name: /登录/ }));
}

beforeEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("AdminApp", () => {
  it("登录后带密码请求，密码不进 URL", async () => {
    const calls = server();
    render(<AdminApp />);
    await login();
    expect(await screen.findByText("内置资料")).toBeInTheDocument();
    expect(new Headers(calls[0].init.headers).get("Authorization")).toBe("Bearer secret");
    expect(calls.every((c) => !c.url.includes("secret"))).toBe(true);
    expect(sessionStorage.getItem("healthpilot-admin")).toBe("secret");
  });

  it("密码错误回到登录页并提示", async () => {
    server(() => json({ detail: "x" }, 401));
    render(<AdminApp />);
    await login("bad");
    expect(await screen.findByText(/密码错误/)).toBeInTheDocument();
    expect(screen.getByLabelText(/密码/)).toBeInTheDocument();
    expect(sessionStorage.getItem("healthpilot-admin")).toBeNull();
  });

  it("后台未启用时提示", async () => {
    server(() => json({ detail: "后台未启用。" }, 503));
    render(<AdminApp />);
    await login();
    expect(await screen.findByText(/后台未启用/)).toBeInTheDocument();
  });

  it("sessionStorage 不可用时也能登录", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    server();
    render(<AdminApp />);
    await login();
    expect(await screen.findByText("内置资料")).toBeInTheDocument();
  });

  it("内置文档显示标签且不能删除", async () => {
    server();
    render(<AdminApp />);
    await login();
    const item = (await screen.findByText("内置资料")).closest("li") as HTMLElement;
    expect(within(item).getByText("内置")).toBeInTheDocument();
    expect(within(item).queryByRole("button", { name: /删除/ })).not.toBeInTheDocument();
  });

  async function uploadDraft() {
    render(<AdminApp />);
    await login();
    await screen.findByText("内置资料");
    const file = new File(["%PDF"], "a.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText(/PDF 文件/), file);
    await userEvent.type(screen.getByLabelText(/标题/), "指南");
    await userEvent.click(screen.getByRole("button", { name: /上传并解析/ }));
    await screen.findByText(/请核对表格和数字后再发布/);
  }

  it("上传后进入草稿，显示页码和可编辑文本", async () => {
    const calls = server();
    await uploadDraft();
    const post = calls.find((c) => c.url === "/api/admin/documents" && c.init.method === "POST")!;
    expect(post.init.body).toBeInstanceOf(FormData);
    expect((post.init.body as FormData).get("title")).toBe("指南");
    expect(screen.getByText(/第 12 页/)).toBeInTheDocument();
    expect(screen.getByDisplayValue("收缩压140")).toBeInTheDocument();
  });

  it("修改后保存，删除一块，发布", async () => {
    const calls = server();
    await uploadDraft();
    fireEvent.change(screen.getByDisplayValue("收缩压140"), { target: { value: "收缩压≥140" } });
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => {
      const patch = calls.find((c) => c.init.method === "PATCH");
      expect(patch?.url).toBe("/api/admin/documents/3/chunks/d3-0");
      expect(JSON.parse(String(patch?.init.body))).toEqual({ text: "收缩压≥140" });
    });
    await userEvent.click(screen.getByRole("button", { name: "发布" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/admin/documents/3/publish" && c.init.method === "POST")).toBe(true));
    expect(await screen.findByText("已发布")).toBeInTheDocument();
  });

  it("发布失败显示原因，仍是草稿", async () => {
    server((url) => (url.endsWith("/publish") ? json({ detail: "向量生成失败，文档保持草稿。" }, 502) : undefined));
    await uploadDraft();
    await userEvent.click(screen.getByRole("button", { name: "发布" }));
    expect(await screen.findByText(/向量生成失败/)).toBeInTheDocument();
    expect(screen.getByText(/请核对表格和数字后再发布/)).toBeInTheDocument();
  });

  it("解析失败显示后端原因", async () => {
    server((url, init) => (url === "/api/admin/documents" && init.method === "POST" ? json({ detail: "没有提取到文字，可能是扫描版，目前不支持。" }, 400) : undefined));
    render(<AdminApp />);
    await login();
    await screen.findByText("内置资料");
    await userEvent.upload(screen.getByLabelText(/PDF 文件/), new File(["x"], "a.pdf", { type: "application/pdf" }));
    await userEvent.type(screen.getByLabelText(/标题/), "指南");
    await userEvent.click(screen.getByRole("button", { name: /上传并解析/ }));
    expect(await screen.findByText(/扫描版/)).toBeInTheDocument();
  });

  it("删除文档要确认", async () => {
    const calls = server((url) => (url === "/api/admin/documents" ? json({ documents: [draft] }) : undefined));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<AdminApp />);
    await login();
    await userEvent.click(within((await screen.findByText("指南")).closest("li") as HTMLElement).getByRole("button", { name: /删除/ }));
    expect(confirm).toHaveBeenCalled();
    expect(calls.some((c) => c.init.method === "DELETE")).toBe(false);
    confirm.mockReturnValue(true);
    await userEvent.click(within(screen.getByText("指南").closest("li") as HTMLElement).getByRole("button", { name: /删除/ }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/admin/documents/3" && c.init.method === "DELETE")).toBe(true));
  });

  describe("向量库", () => {
    async function open() {
      render(<AdminApp />);
      await login();
      await userEvent.click(await screen.findByRole("tab", { name: "向量库" }));
      await screen.findByText("每天吃盐不超过5克");
    }

    it("显示统计和记录", async () => {
      server();
      await open();
      expect(screen.getByText(/pgvector/)).toBeInTheDocument();
      expect(screen.getByText(/qwen3.7-text-embedding/)).toBeInTheDocument();
      expect(screen.getByText(/共 120 条/)).toBeInTheDocument();
      expect(screen.getByText("salt")).toBeInTheDocument();
      expect(screen.getByText(/第 7 页/)).toBeInTheDocument();
      expect(screen.getAllByText(/1024 维/).length).toBeGreaterThan(0);
      expect(screen.getByText(/0\.0123/)).toBeInTheDocument();
    });

    it("搜索和翻页带上参数", async () => {
      const calls = server();
      await open();
      await userEvent.type(screen.getByRole("searchbox"), "盐{Enter}");
      await waitFor(() => expect(calls.some((c) => c.url.startsWith("/api/admin/chunks") && c.url.includes(`q=${encodeURIComponent("盐")}`))).toBe(true));
      await userEvent.click(screen.getByRole("button", { name: "下一页" }));
      await waitFor(() => expect(calls.some((c) => c.url.startsWith("/api/admin/chunks") && /offset=[1-9]/.test(c.url))).toBe(true));
    });

    it("第一页不能再往前", async () => {
      server();
      await open();
      expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    });

    it("没有向量时明确显示", async () => {
      server((url) => (url.startsWith("/api/admin/chunks") ? json({ total: 1, chunks: [{ ...row, dimension: null, vector_preview: null }] }) : undefined));
      await open();
      expect(screen.getByText(/无向量/)).toBeInTheDocument();
    });
  });

  describe("检索测试", () => {
    async function ask() {
      render(<AdminApp />);
      await login();
      await userEvent.click(await screen.findByRole("tab", { name: "检索测试" }));
      await userEvent.type(screen.getByRole("searchbox"), "怎么减盐{Enter}");
    }

    it("显示结果和相似度", async () => {
      server();
      await ask();
      expect(await screen.findByText("每天吃盐不超过5克")).toBeInTheDocument();
      expect(screen.getByText(/0\.71/)).toBeInTheDocument();
    });

    it("没有结果时说明问答会拒答", async () => {
      server((url) => (url.startsWith("/api/admin/search") ? json({ results: [], min_similarity: 0.44 }) : undefined));
      await ask();
      expect(await screen.findByText(/没有达到门槛的结果，问答会拒答/)).toBeInTheDocument();
    });
  });

  it("文件内容里的 HTML 只当文本", async () => {
    server((url) => (url.startsWith("/api/admin/chunks") ? json({ total: 1, chunks: [{ ...row, text: "<img src=x onerror=alert(1)>" }] }) : undefined));
    const { container } = render(<AdminApp />);
    await login();
    await userEvent.click(await screen.findByRole("tab", { name: "向量库" }));
    expect(await screen.findByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });
});
