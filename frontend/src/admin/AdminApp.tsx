import { useCallback, useEffect, useState, type FormEvent } from "react";
import "./admin.css";

type Document = {
  id: number; title: string; source: string; version: string; filename: string;
  status: "draft" | "published"; builtin: boolean; chunk_count: number;
  created_at: string; published_at: string | null; chunks?: DraftChunk[];
};
type DraftChunk = { id: string; seq: number; page: number | null; heading: string; text: string };
type VectorRow = DraftChunk & {
  document_id: number; document_title: string; source: string;
  dimension: number | null; vector_preview: number[] | null;
};
type SearchRow = { id: string; title: string; source: string; page: number | null; text: string; score: number };
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

export default function AdminApp() {
  const [token, setToken] = useState(storedPassword);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [tab, setTab] = useState("文档");
  const [documents, setDocuments] = useState<Document[]>([]);

  const api: Api = useCallback(async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${token}`);
    let response: Response;
    try { response = await fetch(`/api/admin${path}`, { ...init, headers }); }
    catch { throw new Error("网络连接失败，请稍后重试。"); }
    if (response.status === 401) {
      storePassword(""); setToken(""); setPassword(""); setDocuments([]);
      throw new Error("密码错误");
    }
    if (response.status === 503) throw new Error("后台未启用");
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "请求失败，请稍后重试。");
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
    if (token) void refresh().catch(reportError);
  }, [token, refresh, reportError]);

  function login(event: FormEvent) {
    event.preventDefault(); setError(""); storePassword(password); setToken(password);
  }

  return <main className="admin-shell">
    <header className="admin-header">
      <div><a href="/" className="admin-brand">HealthPilot</a><h1>知识库后台</h1><p>内容发布前请人工核对</p></div>
      {token && <button onClick={() => { storePassword(""); setToken(""); setPassword(""); setError(""); setDocuments([]); }}>退出登录</button>}
    </header>
    {error && <p role="alert" className="admin-error">{error}</p>}
    {!token ? <form className="admin-panel admin-login" onSubmit={login}>
      <h2>管理员登录</h2>
      <label>密码<input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required /></label>
      <button className="admin-primary" type="submit">登录</button>
    </form> : <>
      <nav role="tablist" aria-label="后台功能" className="admin-tabs">
        {["文档", "向量库", "检索测试"].map(name => <button key={name} role="tab" aria-selected={tab === name}
          aria-controls="admin-content" onClick={() => { setTab(name); setError(""); }}>{name}</button>)}
      </nav>
      <section role="tabpanel" id="admin-content" aria-label={tab}>
        {tab === "文档" && <Documents api={api} documents={documents} refresh={refresh} reportError={reportError} clearError={() => setError("")} />}
        {tab === "向量库" && <VectorLibrary api={api} documents={documents} reportError={reportError} />}
        {tab === "检索测试" && <SearchTest api={api} reportError={reportError} />}
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

  async function perform(action: () => Promise<void>) {
    clearError(); setBusy(true);
    try { await action(); } catch (reason) { reportError(reason); } finally { setBusy(false); }
  }
  function select(document: Document) {
    setSelected(document); setDirty(new Set()); setSaved(null);
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
      const document = await api<Document>("/documents", { method: "POST", body: data });
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
      const result = await api<Document>(`/documents/${selected.id}/publish`, { method: "POST" });
      setSelected({ ...selected, ...result, chunks: result.chunks ?? selected.chunks });
      await refresh();
    });
    setPublishing(false);
  }

  return <div className="admin-documents">
    <div className="admin-panel">
      <h2>上传 PDF</h2><p className="admin-muted">支持文字版 PDF，最多 20MB、500 页。</p>
      <form onSubmit={upload} className="admin-upload">
        <label>PDF 文件<input name="file" type="file" accept="application/pdf,.pdf" onChange={e => setFile(e.target.files?.[0] ?? null)} /></label>
        <label>标题<input name="title" required /></label>
        <label>来源<input name="source" /></label>
        <label>版本<input name="version" /></label>
        <button className="admin-primary" disabled={busy}>上传并解析</button>
      </form>
      <h2>文档列表</h2>
      {!documents.length && <p className="admin-muted">暂无文档，请上传 PDF。</p>}
      <ul className="admin-list">{documents.map(document => <li key={document.id}>
        <div className="admin-row">
          <button className="admin-link" disabled={busy} onClick={() => void perform(async () => select(await api<Document>(`/documents/${document.id}`)))}>{document.title}</button>
          <span className="admin-badge">{document.builtin ? "内置" : document.status === "draft" ? "草稿" : "已发布"}</span>
        </div>
        <p className="admin-muted">{document.chunk_count} 块{document.version && ` · ${document.version}`}</p>
        {!document.builtin && <button disabled={busy} className="admin-danger" onClick={() => {
          if (!window.confirm(`确认删除“${document.title}”？删除后将无法检索。`)) return;
          void perform(async () => {
            await api(`/documents/${document.id}`, { method: "DELETE" });
            if (selected?.id === document.id) setSelected(null);
            await refresh();
          });
        }}>删除文档</button>}
      </li>)}</ul>
    </div>
    <div className="admin-panel admin-detail">
      {!selected ? <div className="admin-empty"><h2>检查文档内容</h2><p>上传或选择一份文档，查看片段与页码。</p></div> : <>
        <div className="admin-row"><h2>{selected.title}</h2><button disabled={busy} onClick={() => setSelected(null)}>关闭详情</button></div>
        <p className="admin-muted">{selected.source} {selected.version}</p>
        {selected.status === "draft" ? <div className="admin-review">
          <p>请核对表格和数字后再发布</p>
          <button className="admin-primary" disabled={busy} onClick={() => void publish()}>{publishing ? "发布中…" : "发布"}</button>
        </div> : <p className="admin-badge">已发布</p>}
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
        {documents.filter(d => d.status === "published").map(d => <option key={d.id} value={d.id}>{d.title}</option>)}
      </select></label><button>搜索</button>
    </form>
    {loading && <p role="status">正在加载…</p>}
    <ul className="admin-list">{data.chunks.map(row => <li key={row.id}>
      <div className="admin-row"><strong>{row.id}</strong><span>{row.dimension != null ? `${row.dimension} 维` : "无向量"}</span></div>
      <h3>{row.document_title}</h3><p className="admin-muted">{row.source} {pageLabel(row.page)}</p>
      <p className="admin-text">{row.text}</p>
      {row.vector_preview && <details><summary>向量预览（前 8 维）</summary><pre>{row.vector_preview.map(n => n.toFixed(4)).join(", ")}</pre></details>}
    </li>)}</ul>
    {!loading && !data.total && <p>没有匹配的已发布片段。</p>}
    <div className="admin-pagination"><button disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>上一页</button>
      <span>共 {data.total} 条</span><button disabled={loading || offset + limit >= data.total} onClick={() => setOffset(offset + limit)}>下一页</button></div>
  </div>;
}

function SearchTest({ api, reportError }: SectionProps) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<{ results: SearchRow[]; min_similarity: number } | null>(null);
  const [busy, setBusy] = useState(false);
  async function search(event: FormEvent) {
    event.preventDefault(); setBusy(true); setResult(null);
    try { setResult(await api(`/search?${new URLSearchParams({ q: query, k: "4" })}`)); }
    catch (reason) { reportError(reason); } finally { setBusy(false); }
  }
  return <div className="admin-panel">
    <h2>检索测试</h2><p className="admin-muted">使用与问答相同的检索逻辑，检查发布后的内容是否能被找到。</p>
    <form className="admin-filters" onSubmit={event => void search(event)}>
      <label>输入问题<input type="search" value={query} onChange={e => setQuery(e.target.value)} required /></label>
      <button className="admin-primary" disabled={busy || !query.trim()}>{busy ? "检索中…" : "检索"}</button>
    </form>
    {result && <>
      <p>向量相似度门槛：{result.min_similarity}（BM25 回退时不使用门槛）</p>
      {!result.results.length && <p className="admin-review">没有达到门槛的结果，问答会拒答</p>}
      <ul className="admin-list">{result.results.map(row => <li key={row.id}>
        <div className="admin-row"><strong>{row.title}</strong><span>相似度 / BM25 分：{row.score.toFixed(3)}</span></div>
        <p className="admin-muted">{row.id} · {row.source} {pageLabel(row.page)}</p><p className="admin-text">{row.text}</p>
      </li>)}</ul>
    </>}
  </div>;
}
