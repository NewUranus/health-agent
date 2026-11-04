import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import "./admin.css";
import { AdminProblem, ApprovalDialog, ApprovalHistory, type Approval, type AccessPolicy } from "./Approvals";

import { LogExplorer } from "./LogExplorer";
import { Operations } from "./Operations";
import { KnowledgeArchive, TextImport } from "./KnowledgeArchive";
import { KnowledgePolicy, ModelUsage } from "./Governance";

type Document = {
  withdrawn?: boolean; visibility?: "shared" | "restricted"; allowed_user_ids?: string[]; revision?: number;
  id: number; title: string; source: string; version: string; filename: string;
  status: "draft" | "published"; builtin: boolean; chunk_count: number;
  created_at: string; published_at: string | null; chunks?: DraftChunk[];
  variant?: "standard" | "raw" | "clean"; comparison_id?: string;
};
type DraftChunk = { id: string; seq: number; page: number | null; heading: string; text: string };
type VectorRow = DraftChunk & {
  document_id: number; document_title: string; source: string;
  dimension: number | null; vector_preview: number[] | null;
  variant?: Document["variant"];
};
type SearchRow = { id: string; title: string; source: string; page: number | null; text: string; score: number };
type Comparison = {
  groups: { document_id: number; title: string; variant: Document["variant"]; results: SearchRow[] }[];
  mode: "vector" | "bm25"; min_similarity: number;
};
type Stats = {
  backend: string; embedding_model: string | null; dimension: number | null;
  documents: number; published_documents: number; draft_documents: number;
  chunks: number; vectors: number; min_similarity: number;
};
type Api = <T>(path: string, init?: RequestInit) => Promise<T>;
const storageKey = "healthpilot-admin";

function storedPassword() {
  try { return sessionStorage.getItem(storageKey) ?? ""; } catch { return ""; }
}
function storePassword(password: string) {
  try {
    if (password) sessionStorage.setItem(storageKey, password);
    else sessionStorage.removeItem(storageKey);
  } catch { /* 存储受限时仍允许当前页面登录。 */ }
}
function pageLabel(page: number | null | undefined) {
  return page != null ? `第 ${page} 页` : "";
}
function variantLabel(variant: Document["variant"]) {
  return variant === "raw" ? "原始版" : variant === "clean" ? "清洗版" : "";
}
function documentLabel(document: Document) {
  const variant = variantLabel(document.variant);
  return document.title + (variant ? ` · ${variant}` : "");
}

export default function AdminApp() {
  const [token, setToken] = useState(storedPassword);
  // 服务器没配置 ADMIN_PASSWORD 时免登录：先不带密码试一次，再决定是否显示登录页
  const [access, setAccess] = useState<"checking" | "open" | "locked">("checking");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [tab, setTab] = useState("文档");
  const [documents, setDocuments] = useState<Document[]>([]);

  const api: Api = useCallback(async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
    const headers = new Headers(init.headers);
    if (token) headers.set("Authorization", `Bearer ${token}`);
    let response: Response;
    try { response = await fetch(`/api/admin${path}`, { ...init, headers }); }
    catch { throw new Error("网络连接失败，请稍后重试。"); }
    if (response.status === 401) {
      storePassword(""); setToken(""); setPassword(""); setDocuments([]);
      throw new Error("密码错误");
    }
    const body = await response.json();
    if (!response.ok) throw new AdminProblem(typeof body.detail === "string" ? body.detail : "请求失败，请稍后重试。", body.code);
    return body as T;
  }, [token]);

  const reportError = useCallback((reason: unknown) => {
    setError(reason instanceof Error ? reason.message : "操作失败，请稍后重试。");
  }, []);
  const refresh = useCallback(async () => {
    const body = await api<{ documents: Document[] }>("/documents");
    setDocuments(body.documents);
  }, [api]);
  useEffect(() => {
    if (access !== "checking") return;
    const controller = new AbortController();
    fetch("/api/admin/documents", { signal: controller.signal })
      .then((response) => {
        if (controller.signal.aborted) return;
        // 免登录时清掉以前存的密码，不留过期状态
        if (response.ok) { storePassword(""); setToken(""); }
        setAccess(response.ok ? "open" : "locked");
      })
      .catch(() => { if (!controller.signal.aborted) setAccess("locked"); });
    return () => controller.abort();
  }, [access]);
  const entered = access === "open" || (access === "locked" && !!token);
  useEffect(() => {
    if (entered) void refresh().catch(reportError);
  }, [entered, refresh, reportError]);

  function login(event: FormEvent) {
    event.preventDefault(); setError(""); storePassword(password); setToken(password);
  }

  return <main className="admin-shell">
    <header className="admin-header">
      <div><a href="/" className="admin-brand">HealthPilot</a><h1>知识库后台</h1><p>内容发布前请人工核对</p></div>
      {access === "locked" && token && <button onClick={() => { storePassword(""); setToken(""); setPassword(""); setError(""); setDocuments([]); }}>退出登录</button>}
    </header>
    {error && <p role="alert" className="admin-error">{error}</p>}
    {access === "checking" ? <p className="admin-panel">正在连接…</p> : !entered ? <form className="admin-panel admin-login" onSubmit={login}>
      <h2>管理员登录</h2>
      <label>密码<input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required /></label>
      <button className="admin-primary" type="submit">登录</button>
    </form> : <>
      <nav role="tablist" aria-label="后台功能" className="admin-tabs">
        {["文档", "向量库", "检索测试", "确认记录", "模型用量", "版本档案", "文本导入", "运行状态", "日志检索"].map(name => <button key={name} role="tab" aria-selected={tab === name}
          aria-controls="admin-content" onClick={() => { setTab(name); setError(""); }}>{name}</button>)}
      </nav>
      <section role="tabpanel" id="admin-content" aria-label={tab}>
        {tab === "文档" && <Documents api={api} documents={documents} refresh={refresh} reportError={reportError} clearError={() => setError("")} />}
        {tab === "向量库" && <VectorLibrary api={api} documents={documents} reportError={reportError} />}
        {tab === "检索测试" && <SearchTest api={api} documents={documents} reportError={reportError} clearError={() => setError("")} />}
        {tab === "确认记录" && <ApprovalHistory api={api} onChanged={refresh} />}
        {tab === "模型用量" && <ModelUsage api={api} />}
        {tab === "版本档案" && <KnowledgeArchive api={api} />}
        {tab === "文本导入" && <TextImport api={api} onChanged={refresh} />}
        {tab === "日志检索" && <LogExplorer api={api} />}
        {tab === "运行状态" && <Operations api={api} />}
      </section>
    </>}
  </main>;
}

type SectionProps = { api: Api; reportError: (reason: unknown) => void };
function Documents({ api, documents, refresh, reportError, clearError }: SectionProps & {
  documents: Document[]; refresh: () => Promise<void>; clearError: () => void;
}) {
  const [selected, setSelected] = useState<Document | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [dirty, setDirty] = useState<Set<string>>(new Set());
  const [paired, setPaired] = useState(false);
  const [uploadNotice, setUploadNotice] = useState("");
  const [policyOpen, setPolicyOpen] = useState(false);
  const [approval, setApproval] = useState<Approval | null>(null);

  async function requestApproval(documentId: number, action: Approval["action"], access?: AccessPolicy) {
    setApproval(await api<Approval>("/approvals", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_id: documentId, action, access }) }));
  }

  async function perform(action: () => Promise<void>) {
    clearError(); setBusy(true);
    try { await action(); } catch (reason) { reportError(reason); } finally { setBusy(false); }
  }
  function select(document: Document) {
    setPolicyOpen(false); setSelected(document); setDirty(new Set()); setSaved(null);
  }
  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    if (!file) { reportError(new Error("请选择 PDF 文件。")); return; }
    data.set("file", file);
    if (file.size > 20 * 1024 * 1024) {
      reportError(new Error("文件超过 20MB，请拆分后上传。")); return;
    }
    await perform(async () => {
      setUploadNotice("");
      let document: Document;
      if (paired) {
        const result = await api<{ documents: Document[] }>("/document-pairs", { method: "POST", body: data });
        document = result.documents[0];
        setUploadNotice("已生成原始版和清洗版两份草稿。请分别核对、发布，两套向量会保存在同一个库中。");
      } else {
        document = await api<Document>("/documents", { method: "POST", body: data });
      }
      select(document); form.reset(); setFile(null); await refresh();
    });
  }
  async function save(chunk: DraftChunk) {
    if (!selected) return;
    await api(`/documents/${selected.id}/chunks/${chunk.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: chunk.text }),
    });
    setDirty(previous => { const next = new Set(previous); next.delete(chunk.id); return next; });
    setSaved(chunk.id);
  }
  async function publish() {
    if (!selected) return;
    setPublishing(true);
    await perform(async () => {
      // 发布前保存仍在编辑的片段，保证发布的是屏幕上核对过的文本。
      for (const chunk of selected.chunks ?? []) if (dirty.has(chunk.id)) await save(chunk);
      let result: Document;
      try { result = await api<Document>(`/documents/${selected.id}/publish`, { method: "POST" }); }
      catch (reason) {
        if (reason instanceof AdminProblem && reason.code === "APPROVAL_REQUIRED") { await requestApproval(selected.id, "publish"); return; }
        throw reason;
      }
      setSelected({ ...selected, ...result, chunks: result.chunks ?? selected.chunks });
      await refresh();
    });
    setPublishing(false);
  }

  return <div className="admin-documents">
    {approval && <ApprovalDialog key={approval.id} approval={approval} api={api} onClose={() => setApproval(null)} onDone={async row => {
      setApproval(null);
      if (row.status === "succeeded") {
        if (row.action === "delete" && selected?.id === row.document_id) setSelected(null);
        if (row.action !== "delete") select(row.result as Document);
      }
      await refresh();
    }} />}
    <div className="admin-panel">
      <h2>上传 PDF</h2><p className="admin-muted">支持文字版 PDF，最多 20MB、500 页。</p>
      <form onSubmit={upload} className="admin-upload">
        <label>PDF 文件<input name="file" type="file" accept="application/pdf,.pdf" onChange={e => setFile(e.target.files?.[0] ?? null)} /></label>
        <label>标题<input name="title" required /></label>
        <label>来源<input name="source" /></label>
        <label>版本<input name="version" /></label>
        <label>导入方式<select value={paired ? "paired" : "single"} disabled={busy} onChange={e => setPaired(e.target.value === "paired")}>
          <option value="single">普通导入</option>
          <option value="paired">同时生成原始版与清洗版</option>
        </select></label>
        {paired && <p className="admin-muted">同一 PDF 解析一次，按相同的 500 字符上限切块。原始版沿用现有处理，清洗版额外修复列表符号与常见排版断行。</p>}
        <button className="admin-primary" disabled={busy}>上传并解析</button>
      </form>
      {uploadNotice && <p role="status" className="admin-review">{uploadNotice}</p>}
      <h2>文档列表</h2>
      {!documents.length && <p className="admin-muted">暂无文档，请上传 PDF。</p>}
      <ul className="admin-list">{documents.map(document => <li key={document.id}>
        <div className="admin-row">
          <button className="admin-link" disabled={busy} onClick={() => void perform(async () => select(await api<Document>(`/documents/${document.id}`)))}>{documentLabel(document)}</button>
          <span className="admin-badge">{document.withdrawn ? "已撤回" : document.builtin ? "内置" : document.status === "draft" ? "草稿" : "已发布"}</span>
        </div>
        <p className="admin-muted">{document.chunk_count} 块{document.version && ` · ${document.version}`}</p>
        {!document.builtin && <button disabled={busy} className="admin-danger" onClick={() => {
          if (!window.confirm(`确认删除“${document.title}”？删除后将无法检索。`)) return;
          void perform(async () => {
            try { await api(`/documents/${document.id}`, { method: "DELETE" }); }
            catch (reason) {
              if (reason instanceof AdminProblem && reason.code === "APPROVAL_REQUIRED") { await requestApproval(document.id, "delete"); return; }
              throw reason;
            }
            if (selected?.id === document.id) setSelected(null);
            await refresh();
          });
        }}>删除文档</button>}
      </li>)}</ul>
    </div>
    <div className="admin-panel admin-detail">
      {!selected ? <div className="admin-empty"><h2>检查文档内容</h2><p>上传或选择一份文档，查看片段与页码。</p></div> : <>
        <div className="admin-row"><h2>{documentLabel(selected)}</h2><button disabled={busy} onClick={() => setSelected(null)}>关闭详情</button></div>
        <p className="admin-muted">{selected.source} {selected.version}</p>
        {selected.status === "draft" ? <div className="admin-review">
          <p>请核对表格和数字后再发布</p>
          <button className="admin-primary" disabled={busy} onClick={() => void publish()}>{publishing ? "发布中…" : "发布"}</button>
        </div> : <p className="admin-badge">{selected.withdrawn ? "已撤回" : "已发布"}</p>}
        <div className="admin-actions">
          <button disabled={busy} onClick={() => setPolicyOpen(value => !value)}>访问范围</button>
          {selected.status === "published" && !selected.withdrawn && <button disabled={busy} onClick={() => void perform(() => requestApproval(selected.id, "withdraw"))}>撤回文档</button>}
        </div>
        {policyOpen && <KnowledgePolicy key={selected.id} api={api} initial={{ visibility: selected.visibility ?? "shared", allowed_user_ids: selected.allowed_user_ids ?? [] }}
          busy={busy} onSubmit={policy => perform(() => requestApproval(selected.id, "access", policy))} />}
        {selected.chunks?.map(chunk => <article className="admin-chunk" key={chunk.id}>
          <p className="admin-muted">{pageLabel(chunk.page)} · {chunk.id}</p><h3>{chunk.heading}</h3>
          {selected.status === "draft" ? <>
            <label>片段文本<textarea value={chunk.text} disabled={busy} onChange={e => {
              setSelected({ ...selected, chunks: selected.chunks?.map(c => c.id === chunk.id ? { ...c, text: e.target.value } : c) });
              setDirty(previous => new Set(previous).add(chunk.id)); setSaved(null);
            }} /></label>
            <div className="admin-actions"><button disabled={busy || !chunk.text.trim()} onClick={() => void perform(() => save(chunk))}>保存</button>
              <button disabled={busy} className="admin-danger" onClick={() => void perform(async () => {
                await api(`/documents/${selected.id}/chunks/${chunk.id}`, { method: "DELETE" });
                setSelected({ ...selected, chunks: selected.chunks?.filter(c => c.id !== chunk.id), chunk_count: selected.chunk_count - 1 });
                await refresh();
              })}>删除</button>
              {saved === chunk.id && <span role="status">已保存</span>}
            </div>
          </> : <p className="admin-text">{chunk.text}</p>}
        </article>)}
        {!selected.chunks?.length && <p>文档没有片段，请重新上传。</p>}
      </>}
    </div>
  </div>;
}

function VectorLibrary({ api, documents, reportError }: SectionProps & { documents: Document[] }) {
  const [stats, setStats] = useState<Stats | null>(null);
  const [data, setData] = useState<{ total: number; chunks: VectorRow[] }>({ total: 0, chunks: [] });
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  const [documentId, setDocumentId] = useState("");
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const limit = 50;
  useEffect(() => {
    let active = true;
    void api<Stats>("/stats").then(value => { if (active) setStats(value); }).catch(reportError);
    return () => { active = false; };
  }, [api, reportError]);
  useEffect(() => {
    let active = true;
    setLoading(true);
    const params = new URLSearchParams({ q, limit: String(limit), offset: String(offset) });
    if (documentId) params.set("document_id", documentId);
    void api<typeof data>(`/chunks?${params}`).then(value => { if (active) setData(value); })
      .catch(reason => { if (active) reportError(reason); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [api, q, documentId, offset, reportError]);
  return <div className="admin-panel">
    <h2>向量库</h2>
    {stats && <dl className="admin-stats">
      <div><dt>后端</dt><dd>{stats.backend}</dd></div><div><dt>模型</dt><dd>{stats.embedding_model ?? "未配置"}</dd></div>
      <div><dt>维度</dt><dd>{stats.dimension != null ? `${stats.dimension} 维` : "暂无向量"}</dd></div>
      <div><dt>文档数</dt><dd>{stats.documents}（已发布 {stats.published_documents} / 草稿 {stats.draft_documents}）</dd></div>
      <div><dt>向量数</dt><dd>{stats.vectors}</dd></div><div><dt>门槛</dt><dd>{stats.min_similarity}</dd></div>
    </dl>}
    <form className="admin-filters" onSubmit={e => { e.preventDefault(); setQ(input); setOffset(0); }}>
      <label>片段文本搜索<input type="search" value={input} onChange={e => setInput(e.target.value)} placeholder="输入文本关键词" /></label>
      <label>文档筛选<select value={documentId} onChange={e => { setDocumentId(e.target.value); setOffset(0); }}>
        <option value="">全部已发布文档</option>
        {documents.filter(d => d.status === "published").map(d => <option key={d.id} value={d.id}>{documentLabel(d)}</option>)}
      </select></label><button>搜索</button>
    </form>
    {loading && <p role="status">正在加载…</p>}
    <ul className="admin-list">{data.chunks.map(row => <li key={row.id}>
      <div className="admin-row"><strong>{row.id}</strong><span>{row.dimension != null ? `${row.dimension} 维` : "无向量"}</span></div>
      <h3>{row.document_title}{variantLabel(row.variant) && ` · ${variantLabel(row.variant)}`}</h3><p className="admin-muted">{row.source} {pageLabel(row.page)}</p>
      <p className="admin-text">{row.text}</p>
      {row.vector_preview && <details><summary>向量预览（前 8 维）</summary><pre>{row.vector_preview.map(n => n.toFixed(4)).join(", ")}</pre></details>}
    </li>)}</ul>
    {!loading && !data.total && <p>没有匹配的已发布片段。</p>}
    <div className="admin-pagination"><button disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>上一页</button>
      <span>共 {data.total} 条</span><button disabled={loading || offset + limit >= data.total} onClick={() => setOffset(offset + limit)}>下一页</button></div>
  </div>;
}

function SearchTest({ api, documents, reportError, clearError }: SectionProps & { documents: Document[]; clearError: () => void }) {
  const [query, setQuery] = useState("");
  const [documentId, setDocumentId] = useState("");
  const [result, setResult] = useState<{ results: SearchRow[]; min_similarity: number } | null>(null);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  const published = documents.filter(document => document.status === "published");
  const selected = published.find(document => String(document.id) === documentId);
  const pairReady = !!selected?.comparison_id && published.some(document =>
    document.id !== selected.id && document.comparison_id === selected.comparison_id && document.variant !== selected.variant);
  useEffect(() => () => request.current?.abort(), []);
  async function search(event?: FormEvent, compare = false) {
    event?.preventDefault();
    if (request.current || !query.trim()) return;
    const controller = new AbortController();
    request.current = controller;
    clearError(); setBusy(true); setResult(null); setComparison(null);
    const params = new URLSearchParams({ q: query.trim(), k: "4" });
    if (documentId) params.set("document_id", documentId);
    try {
      if (compare) {
        const value = await api<Comparison>(`/compare?${params}`, { signal: controller.signal });
        if (!controller.signal.aborted) setComparison(value);
      } else {
        const value = await api<NonNullable<typeof result>>(`/search?${params}`, { signal: controller.signal });
        if (!controller.signal.aborted) setResult(value);
      }
    } catch (reason) {
      if (!controller.signal.aborted) reportError(reason);
    } finally {
      request.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return <div className="admin-panel">
    <h2>检索测试</h2><p className="admin-muted">使用与问答相同的检索逻辑，可选择一份已发布 PDF，仅查看该文档中的搜索结果。</p>
    <form className="admin-filters" onSubmit={event => void search(event)}>
      <label>输入问题<input type="search" value={query} disabled={busy} onChange={e => { setQuery(e.target.value); setResult(null); setComparison(null); }} required /></label>
      <label>检索范围<select value={documentId} disabled={busy} onChange={e => { setDocumentId(e.target.value); setResult(null); setComparison(null); clearError(); }}>
        <option value="">全部已发布文档</option>
        <optgroup label="已发布 PDF">{published.filter(document => !document.builtin).map(document =>
          <option key={document.id} value={document.id}>{documentLabel(document)}{document.version && ` · ${document.version}`}（{document.filename}）</option>)}</optgroup>
        <optgroup label="内置资料">{published.filter(document => document.builtin).map(document =>
          <option key={document.id} value={document.id}>{document.title}</option>)}</optgroup>
      </select></label>
      <button className="admin-primary" disabled={busy || !query.trim()}>{busy ? "检索中…" : "检索"}</button>
      {selected?.comparison_id && <button type="button" disabled={busy || !query.trim() || !pairReady}
        onClick={() => void search(undefined, true)}>对比两版</button>}
    </form>
    {selected?.comparison_id && !pairReady && <p className="admin-muted">同次上传的原始版和清洗版都发布后，即可并排对比。</p>}
    {result && <>
      <p>检索范围：{selected ? documentLabel(selected) : "全部已发布文档"}</p>
      <p>向量相似度门槛：{result.min_similarity}（BM25 回退时不使用门槛）</p>
      {!result.results.length && <p className="admin-review">{documentId ? "所选文档中没有符合检索条件的结果，可尝试其他问题或切换到全部文档。" : "没有达到门槛的结果，问答会拒答"}</p>}
      <ul className="admin-list">{result.results.map(row => <li key={row.id}>
        <div className="admin-row"><strong>{row.title}</strong><span>相似度 / BM25 分：{row.score.toFixed(3)}</span></div>
        <p className="admin-muted">{row.id} · {row.source} {pageLabel(row.page)}</p><p className="admin-text">{row.text}</p>
      </li>)}</ul>
    </>}
    {comparison && <>
      <p>检索方式：{comparison.mode === "vector" ? `向量相似度（两版门槛均为 ${comparison.min_similarity}）` : "BM25（两版均未使用向量检索，不使用相似度门槛）"}</p>
      <div className="admin-comparison">{comparison.groups.map(group => <section key={group.document_id} aria-label={variantLabel(group.variant)}>
        <h3>{variantLabel(group.variant)} · {group.title}</h3>
        {!group.results.length && <p>没有符合条件的结果。</p>}
        <ol className="admin-list">{group.results.map((row, index) => <li key={row.id}>
          <div className="admin-row"><strong>第 {index + 1} 条</strong><span>{comparison.mode === "vector" ? "相似度" : "BM25 分"}：{row.score.toFixed(3)}</span></div>
          <p className="admin-muted">{row.id} · {row.source} {pageLabel(row.page)}</p>
          <p className="admin-text">{row.text}</p>
        </li>)}</ol>
      </section>)}</div>
    </>}
  </div>;
}
