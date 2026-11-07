import { useEffect, useState } from "react";
import type { AdminApi } from "./Approvals";

type Overview = { release: string; tasks: Record<string, number>; workers: { id: string; slots: number; last_seen: number }[];
  oldest_pending_seconds: number; stale_tasks: number; completed_24h: number; average_completion_seconds_24h: number;
  needs_support: number; admission_paused: boolean; costs_24h: { cost_microusd: number | null; priced_calls: number };
  alerts: { code: string; message: string; level: string }[]; failures_24h: { error_code: string; count: number }[];
  // 旧版后端不返回缓存统计；滚动发布期间前端可能先于后端更新
  cache?: { tools: CacheCounter; vectors: CacheCounter } };
type CacheCounter = { hits: number; misses: number; saved: number };
type Audit = { id: string; at: number; action: string; actor: string; resource: string; request_id: string; status: number };
type Queued = { id: string; status: string; priority: number; request_id: string };
type SupportTask = { id: string; status: string; error_code: string | null; request_id: string; username: string; created_at: number; reviewed_at: number };
const statuses: Record<string, string> = { pending: "排队", running: "执行中", waiting_human: "等待回答", succeeded: "成功", failed: "失败", cancelled: "取消", timed_out: "超时", interrupted: "中断" };

export function Operations({ api }: { api: AdminApi }) {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [queue, setQueue] = useState<Queued[]>([]);
  const [support, setSupport] = useState<SupportTask[]>([]);
  const [records, setRecords] = useState<Audit[]>([]);
  const [requestId, setRequestId] = useState("");
  const [filter, setFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const [next, queued, waiting] = await Promise.all([
          api<Overview>("/operations", { signal: controller.signal }),
          api<{ tasks: Queued[] }>("/queue", { signal: controller.signal }),
          api<{ tasks: SupportTask[] }>("/support", { signal: controller.signal }),
        ]);
        if (!controller.signal.aborted) { setOverview(next); setQueue(queued.tasks); setSupport(waiting.tasks); setError(""); timer = setTimeout(() => void refresh(), 10000); }
      } catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "运行状态读取失败。"); }
    }
    void refresh(); return () => { controller.abort(); clearTimeout(timer); };
  }, [api, revision]);
  useEffect(() => {
    const controller = new AbortController();
    void api<{ records: Audit[] }>(`/audit?request_id=${encodeURIComponent(filter)}&offset=${offset}`, { signal: controller.signal })
      .then(body => { if (!controller.signal.aborted) setRecords(body.records); })
      .catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [api, filter, offset, revision]);
  async function resolveSupport(id: string) {
    setBusy(true);
    try { await api(`/support/${id}/resolve`, { method: "POST" }); setRevision(value => value + 1); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "协助标记未更新。"); }
    finally { setBusy(false); }
  }
  async function mutate(path: string, body: object) {
    setBusy(true);
    try { await api(path, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); setRevision(value => value + 1); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "操作未完成。"); }
    finally { setBusy(false); }
  }
  return <section className="admin-panel"><h2>运行状态</h2><p>每 10 秒刷新。告警仅显示在此处；外部通知需接入 Prometheus / Alertmanager。</p>
    <button onClick={() => setRevision(value => value + 1)}>刷新运行状态</button>{error && <p role="alert" className="admin-error">{error}</p>}
    {overview && <><p>版本 {overview.release} · 当前接收：{overview.admission_paused ? "已暂停" : "正常"}</p>
      <button disabled={busy} onClick={() => { if (window.confirm(overview.admission_paused ? "恢复接收新任务？" : "暂停接收新任务？已经接收的任务将继续执行。")) void mutate("/admission", { paused: !overview.admission_paused }); }}>{overview.admission_paused ? "恢复新任务接收" : "暂停新任务接收"}</button>
      <dl className="admin-stats"><div><dt>有效 Worker / 执行槽</dt><dd>{overview.workers.length} / {overview.workers.reduce((sum, worker) => sum + worker.slots, 0)}</dd></div>
        <div><dt>最久排队</dt><dd>{overview.oldest_pending_seconds} 秒</dd></div><div><dt>24 小时完成量</dt><dd>{overview.completed_24h}</dd></div>
        <div><dt>24 小时平均完成耗时（含排队）</dt><dd>{overview.average_completion_seconds_24h.toFixed(1)} 秒</dd></div>
        <div><dt>需人工协助标记</dt><dd>{overview.needs_support}</dd></div><div><dt>24 小时已知费用估算（美元）</dt><dd>{overview.costs_24h.cost_microusd == null ? "未知" : (overview.costs_24h.cost_microusd / 1000000).toFixed(6)}</dd></div></dl>
      {overview.alerts.map(alert => <p role="alert" className="admin-review" key={alert.code}>{alert.code}：{alert.message}</p>)}
      <p>{Object.entries(overview.tasks).map(([key, value]) => `${statuses[key] ?? key} ${value}`).join(" · ")}</p>
      <h3>计算缓存</h3><p>相同的风险计算和查询向量会在重启和多个 Worker 之间复用。网页搜索不进入缓存。省下的调用等于命中次数。</p>
      {overview.cache ? <dl className="admin-stats"><div><dt>风险计算命中 / 未命中 / 省下的调用</dt><dd>{overview.cache.tools.hits} / {overview.cache.tools.misses} / {overview.cache.tools.saved}</dd></div>
        <div><dt>查询向量命中 / 未命中 / 省下的调用</dt><dd>{overview.cache.vectors.hits} / {overview.cache.vectors.misses} / {overview.cache.vectors.saved}</dd></div></dl>
        : <p>当前服务端版本未提供缓存统计。</p>}
      <h3>最近 24 小时失败分类</h3><ul>{overview.failures_24h.map(row => <li key={row.error_code}>{row.error_code || "未分类"}：{row.count}</li>)}</ul>
    </>}
    <h3>人工协助</h3><p>只显示用户标记为需要协助的任务编号和账号，不包含健康正文。标记已处理后从队列移除。</p>
    <ul className="admin-list">{support.map(task => <li key={task.id}><strong>{task.username}</strong> · {statuses[task.status] ?? task.status}
      <small>{task.error_code || "未分类"} · {task.id.slice(0, 8)} · {new Date(task.reviewed_at * 1000).toLocaleString("zh-CN")}</small>
      <button type="button" disabled={busy} onClick={() => void resolveSupport(task.id)}>标记已处理</button></li>)}
      {!support.length && <li>当前没有待处理的人工协助。</li>}</ul>
    <h3>任务调度</h3><p>优先级 0–9，数字越大越优先；同账号仍串行执行。</p>
    <ul className="admin-list">{queue.map(task => <li key={task.id}><strong>{task.id.slice(0, 8)}</strong> · {statuses[task.status] ?? task.status}
      <label>调度优先级<select value={task.priority} disabled={busy || task.status !== "pending"} onChange={event => void mutate(`/queue/${task.id}/priority`, { priority: Number(event.target.value) })}>
        {Array.from({ length: 10 }, (_, index) => <option key={index} value={index}>{index}</option>)}</select></label><small>请求编号 {task.request_id}</small></li>)}</ul>
    <h3>请求审计</h3><form onSubmit={event => { event.preventDefault(); setOffset(0); setFilter(requestId); }}><input aria-label="查询请求编号" value={requestId} maxLength={128} onChange={event => setRequestId(event.target.value)} placeholder="按 request_id 查询" /><button>查询审计</button></form>
    <ul className="admin-list">{records.map(row => <li key={row.id}><strong>{row.action}</strong> · {row.status}<p>{row.actor || "匿名"} · {row.resource}</p><small>{new Date(row.at * 1000).toLocaleString("zh-CN")} · {row.request_id}</small></li>)}</ul>
    <button disabled={!offset} onClick={() => setOffset(value => Math.max(0, value - 100))}>上一页</button><button disabled={records.length < 100} onClick={() => setOffset(value => value + 100)}>下一页</button>
  </section>;
}
