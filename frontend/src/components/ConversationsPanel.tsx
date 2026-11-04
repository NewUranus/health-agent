import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/api";
import { responseProblem } from "../lib/tasks";
import type { Conversation, ConversationPage } from "../lib/conversations";

export function ConversationsPanel({ busy, onOpen }: { busy: boolean; onOpen: (page: ConversationPage) => void }) {
  const [rows, setRows] = useState<Conversation[]>([]);
  const [archived, setArchived] = useState(false);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [edit, setEdit] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [page, setPage] = useState<ConversationPage | null>(null);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  useEffect(() => {
    const controller = new AbortController();
    void apiFetch(`/api/v1/conversations?archived=${archived}&offset=${offset}&q=${encodeURIComponent(search)}`, { signal: controller.signal })
      .then(async response => { if (!response.ok) throw await responseProblem(response); return response.json(); })
      .then(body => { if (!controller.signal.aborted) { setRows(body.conversations); setError(""); } })
      .catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [archived, offset, search, revision]);
  async function update(id: string, body: { title?: string; archived?: boolean }) {
    setLoading(true);
    try {
      const response = await apiFetch(`/api/v1/conversations/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!response.ok) throw await responseProblem(response);
      setEdit(null); setRevision(value => value + 1); setError("");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "更新失败。"); }
    finally { setLoading(false); }
  }
  async function open(id: string, before?: string) {
    active.current?.abort(); const controller = new AbortController(); active.current = controller;
    setLoading(true); setError("");
    try {
      const response = await apiFetch(`/api/v1/conversations/${id}/turns${before ? `?before=${encodeURIComponent(before)}` : ""}`, { signal: controller.signal });
      if (!response.ok) throw await responseProblem(response);
      const body: ConversationPage = await response.json();
      if (!controller.signal.aborted) { setPage(body); onOpen(body); }
    } catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "会话读取失败。"); }
    finally { if (!controller.signal.aborted) setLoading(false); }
  }
  async function download(id: string) {
    setLoading(true);
    try {
      const response = await apiFetch(`/api/v1/conversations/${id}/export`);
      if (!response.ok) throw await responseProblem(response);
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = `healthpilot-${id}.md`; anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "导出失败。"); }
    finally { setLoading(false); }
  }
  return <section className="task-list"><h3>我的会话</h3>
    <form onSubmit={event => { event.preventDefault(); setOffset(0); setSearch(query); }} className="conversation-search">
      <input aria-label="搜索会话标题" value={query} maxLength={80} onChange={event => setQuery(event.target.value)} placeholder="搜索标题" /><button>搜索</button>
    </form>
    <label><input type="checkbox" checked={archived} onChange={event => { setArchived(event.target.checked); setOffset(0); }} />查看已归档</label>
    <button onClick={() => setRevision(value => value + 1)}>刷新列表</button>
    <p>归档不删除记录，也不会取消正在执行的任务。导出仅包含最近 50 轮和当前有权查看的来源。</p>
    {error && <p role="alert" className="warning">{error}</p>}
    {!rows.length && <p>暂无会话。提交第一条消息后自动保存。</p>}
    {rows.map(row => <article className="saved-task" key={row.id}><strong>{row.title}</strong><small>{row.turns} 轮 · {new Date(row.updated_at).toLocaleString("zh-CN")}</small>
      {edit === row.id ? <form onSubmit={event => { event.preventDefault(); void update(row.id, { title }); }}><input aria-label="新会话标题" maxLength={80} value={title} onChange={event => setTitle(event.target.value)} required /><button disabled={loading || !title.trim()}>保存名称</button><button type="button" onClick={() => setEdit(null)}>取消</button></form>
        : <button disabled={loading} onClick={() => { setEdit(row.id); setTitle(row.title); }}>重命名</button>}
      <button disabled={busy || loading} onClick={() => void open(row.id)}>{row.archived ? "查看历史" : "打开会话"}</button>
      <button disabled={loading} onClick={() => void update(row.id, { archived: !row.archived })}>{row.archived ? "恢复会话" : "归档"}</button>
      <button disabled={loading} onClick={() => void download(row.id)}>导出 Markdown</button>
    </article>)}
    {page?.next_cursor && <button disabled={busy || loading} onClick={() => void open(page.conversation.id, page.next_cursor!)}>查看更早的 20 轮</button>}
    {page?.turns.some(turn => turn.truncated) && <p className="warning">部分轮次过程较长，请到任务记录读取完整过程。</p>}
    <div><button disabled={!offset} onClick={() => setOffset(value => Math.max(0, value - 30))}>上一页</button><button disabled={rows.length < 30} onClick={() => setOffset(value => value + 30)}>下一页</button></div>
  </section>;
}
