import { useEffect, useRef, useState, type FormEvent } from "react";
import type { AdminApi } from "./Approvals";

type Log = { id: string; at: number; level: string; service: string; instance: string; event: string; message: string;
  trace_id: string; span_id: string; parent_span_id: string; request_id: string; task_id: string;
  attributes: Record<string, string | number | boolean> };
type Page = { records: Log[]; next_cursor: string | null; start: number; end: number };
type Health = { central_available: boolean; central_backend?: string; pending?: number; dropped?: number; export_error?: string;
  services: { service: string; last_seen: number }[] };
const time = (value: number) => new Date(value).toLocaleString();
const label: Record<string, string> = { trace_id: "链路 ID", request_id: "请求 ID", task_id: "任务 ID", q: "关键词", event: "事件" };

export function LogExplorer({ api }: { api: AdminApi }) {
  const [filters, setFilters] = useState({ trace_id: "", request_id: "", task_id: "", q: "", event: "", service: "", level: "", hours: "24" });
  const [page, setPage] = useState<Page | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [cursors, setCursors] = useState<string[]>([]);
  const [trace, setTrace] = useState<Log[] | null>(null);
  const [traceId, setTraceId] = useState("");
  const [traceCursor, setTraceCursor] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);

  async function load(params: string, history: string[]) {
    controller.current?.abort();
    const request = new AbortController(); controller.current = request;
    setBusy(true); setError("");
    try {
      const [result, state] = await Promise.all([api<Page>(`/logs?${params}`, { signal: request.signal }), api<Health>("/logs/status", { signal: request.signal })]);
      if (request.signal.aborted) return;
      setPage(result); setHealth(state); setCursors(history);
    } catch (e) { if (!request.signal.aborted) setError(e instanceof Error ? e.message : "日志查询失败"); }
    finally { if (!request.signal.aborted) setBusy(false); }
  }
  function search(event: FormEvent) {
    event.preventDefault(); const end = Date.now();
    const params = new URLSearchParams({ start: String(end - Number(filters.hours) * 3600000), end: String(end), limit: "100" });
    for (const [key, value] of Object.entries(filters)) if (key !== "hours" && value.trim()) params.set(key, value.trim());
    setQuery(params.toString()); setTrace(null); void load(params.toString(), []);
  }
  async function openTrace(id: string, more = false) {
    setBusy(true); setError("");
    controller.current?.abort(); const request = new AbortController(); controller.current = request;
    try {
      const result = await api<Page>(`/traces/${id}${more && traceCursor ? `?before=${encodeURIComponent(traceCursor)}` : ""}`, { signal: request.signal });
      if (request.signal.aborted) return;
      setTrace(old => more ? [...(old || []), ...result.records] : result.records);
      setTraceId(id); setTraceCursor(result.next_cursor);
    } catch (e) { if (!request.signal.aborted) setError(e instanceof Error ? e.message : "链路查询失败"); }
    finally { if (!request.signal.aborted) setBusy(false); }
  }
  function download() {
    const records = trace ?? page?.records ?? [];
    const url = URL.createObjectURL(new Blob([records.map(item => JSON.stringify(item)).join("\n") + "\n"], { type: "application/x-ndjson" }));
    const link = document.createElement("a"); link.href = url; link.download = trace ? "healthpilot-trace.jsonl" : "healthpilot-logs-page.jsonl"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const spans = new Map<string, Log>();
  for (const log of [...(trace || [])].sort((a, b) => a.at - b.at || (a.event === "span.finished" ? 1 : -1))) {
    if (log.event.startsWith("span.")) spans.set(log.span_id, log);
  }
  function depth(log: Log) {
    let parent = log.parent_span_id, count = 0; const visited = new Set<string>();
    while (parent && spans.has(parent) && !visited.has(parent) && count < 12) { visited.add(parent); count++; parent = spans.get(parent)!.parent_span_id; }
    return count;
  }
  return <div className="admin-panel log-explorer">
    <h2>集中日志检索</h2><p>按请求、任务或链路追踪执行过程。日志不包含健康正文、工具参数或密钥。</p>
    <form onSubmit={search} className="log-filters">
      <label>时间范围<select value={filters.hours} onChange={e => setFilters({ ...filters, hours: e.target.value })}><option value="1">最近一小时</option><option value="24">最近一天</option><option value="168">最近七天</option><option value="720">最近三十天</option></select></label>
      <label>服务<select value={filters.service} onChange={e => setFilters({ ...filters, service: e.target.value })}>{["", "api", "worker", "mcp"].map(x => <option key={x} value={x}>{x || "全部"}</option>)}</select></label>
      <label>级别<select value={filters.level} onChange={e => setFilters({ ...filters, level: e.target.value })}>{["", "INFO", "WARNING", "ERROR", "CRITICAL"].map(x => <option key={x} value={x}>{x || "全部"}</option>)}</select></label>
      {Object.entries(label).map(([key, title]) => <label key={key}>{title}<input value={filters[key as keyof typeof filters]} maxLength={key.endsWith("_id") ? 32 : 100} pattern={key.endsWith("_id") ? "[0-9a-f]{32}" : undefined} onChange={e => setFilters({ ...filters, [key]: e.target.value })} /></label>)}
      <button disabled={busy} className="admin-primary">{busy ? "查询中…" : "查询"}</button>
    </form>
    {error && <p role="alert" className="admin-error">{error}</p>}
    {health && <p role="status">日志中心：{health.central_available ? "可用" : "不可用"}（{health.central_backend || "未知"}）；本实例缓冲：{health.pending ?? "未知"}；丢弃：{health.dropped ?? "未知"}。{health.export_error && `补传状态：${health.export_error}`}<br />{health.services.map(s => `${s.service} 最近入库 ${time(s.last_seen)}`).join("；")}</p>}
    {(page || trace) && <button onClick={download}>导出{trace ? "已加载链路" : "当前页"}</button>}
    {trace ? <section><h3>链路 {traceId}</h3><p>耗时包含子步骤；未结束可能仍在执行、补传或发生进程中断。跨机器时间取决于时钟同步。</p><button onClick={() => setTrace(null)}>返回日志</button>
      {[...spans.values()].sort((a, b) => (a.at - Number(a.attributes.duration_ms || 0)) - (b.at - Number(b.attributes.duration_ms || 0))).map(log => <div className="log-span" key={log.span_id} style={{ marginLeft: depth(log) * 18 }}><strong>{String(log.attributes.name)}</strong> · {log.service} · {log.event === "span.started" ? "尚无结束记录" : String(log.attributes.status)} · {String(log.attributes.duration_ms ?? "—")} ms{log.parent_span_id && !spans.has(log.parent_span_id) && " · 父步骤尚未加载或已过期"}<details><summary>标识与元数据</summary><pre>{JSON.stringify(log, null, 2)}</pre></details></div>)}
      {traceCursor && <button disabled={busy} onClick={() => void openTrace(traceId, true)}>继续加载较早链路</button>}
      {!spans.size && <p>暂未查到步骤，日志可能正在补传。</p>}
    </section> : <><div className="log-table"><table><thead><tr><th>时间 / 级别</th><th>服务 / 事件</th><th>链路与元数据</th></tr></thead><tbody>{page?.records.map(log => <tr key={log.id}><td>{time(log.at)}<br />{log.level}</td><td>{log.service}<br />{log.event}</td><td>{log.trace_id && <button disabled={busy} onClick={() => void openTrace(log.trace_id)}>查看链路</button>}<details><summary>{String(log.attributes.name || log.message || "详情")}</summary><pre>{JSON.stringify(log, null, 2)}</pre></details></td></tr>)}</tbody></table></div>
      {page && !page.records.length && <p>当前条件没有日志，可扩大时间范围或检查日志中心状态。</p>}
      {!!cursors.length && <button disabled={busy} onClick={() => { const previous = cursors.slice(0, -1); void load(query + (previous.length ? `&before=${encodeURIComponent(previous[previous.length - 1])}` : ""), previous); }}>上一页</button>}
      {page?.next_cursor && <button disabled={busy} onClick={() => void load(`${query}&before=${encodeURIComponent(page.next_cursor!)}`, [...cursors, page.next_cursor!])}>下一页</button>}
    </>}
  </div>;
}
