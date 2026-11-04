import { useEffect, useState, type FormEvent } from "react";
import type { AdminApi } from "./Approvals";

type Archive = { id: string; title: string; version: string; reason: string; archived_at: string;
  document?: { chunks: { id: string; page: number | null; text: string }[] } };
const reasons: Record<string, string> = { publish: "发布", access: "访问范围调整", withdraw: "撤回" };
export function KnowledgeArchive({ api }: { api: AdminApi }) {
  const [rows, setRows] = useState<Archive[]>([]);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Archive | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void api<{ archives: Archive[] }>(`/knowledge-archives?offset=${offset}`, { signal: controller.signal })
      .then(body => { if (!controller.signal.aborted) { setRows(body.archives); setError(""); } })
      .catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [api, offset]);
  async function open(id: string) {
    try { setSelected(await api<Archive>(`/knowledge-archives/${id}`)); setError(""); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "读取失败。"); }
  }
  return <section className="admin-panel"><h2>知识版本档案</h2><p>保留确认变更时的版本，供管理员核对；不参与用户检索。删除文档会同时删除该文档的档案。</p>
    {error && <p role="alert">{error}</p>}
    {!rows.length && <p>暂无版本档案，新确认的知识变更会自动归档。</p>}
    <ul className="admin-list">{rows.map(row => <li key={row.id}><button onClick={() => void open(row.id)}>{row.title} · {row.version || "未标版本"}</button>
      <p>{reasons[row.reason] ?? row.reason} · {new Date(row.archived_at).toLocaleString("zh-CN")}</p></li>)}</ul>
    <button disabled={!offset} onClick={() => setOffset(value => Math.max(0, value - 50))}>上一页</button>
    <button disabled={rows.length < 50} onClick={() => setOffset(value => value + 50)}>下一页</button>
    {selected && <section><h3>{selected.title} · {selected.version}</h3><button onClick={() => setSelected(null)}>关闭原文</button>
      {selected.document?.chunks.map(chunk => <article className="admin-chunk" key={chunk.id}><small>{chunk.id}{chunk.page != null && ` · 第 ${chunk.page} 页`}</small><p className="admin-text">{chunk.text}</p></article>)}</section>}
  </section>;
}

export function TextImport({ api, onChanged }: { api: AdminApi; onChanged: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const values = new FormData(form);
    setBusy(true); setMessage("");
    try {
      await api("/text-documents", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.fromEntries(values)) });
      form.reset(); setMessage("草稿已创建，请到文档页核对访问范围和正文后发布。"); await onChanged();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "导入失败。"); }
    finally { setBusy(false); }
  }
  return <section className="admin-panel"><h2>文本导入</h2><p>粘贴纯文本或 Markdown，最多 20 万字符。导入后不会自动发布。</p>
    <form onSubmit={event => void submit(event)} className="admin-upload">
      <label>标题<input name="title" required maxLength={200} /></label><label>来源<input name="source" maxLength={2000} /></label>
      <label>版本<input name="version" maxLength={100} /></label><label>正文<textarea name="text" required maxLength={200000} rows={12} /></label>
      <button disabled={busy}>{busy ? "正在创建…" : "创建待审核草稿"}</button>
    </form>{message && <p role="status">{message}</p>}
  </section>;
}
