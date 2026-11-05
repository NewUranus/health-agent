import { useEffect, useState } from "react";
import { apiFetch } from "../lib/api";
import { responseProblem, type SavedTask } from "../lib/tasks";
import type { ServerEvent } from "../types";

const labels: Record<string, string> = { pending: "排队中", running: "执行中", waiting_human: "等待随访回答", succeeded: "已完成",
  failed: "执行失败", cancelled: "已取消", timed_out: "已超时", interrupted: "执行中断，需核对" };

export function TasksPanel({ onResume, busy }: { onResume: (task: SavedTask, events: ServerEvent[]) => void; busy: boolean }) {
  const [tasks, setTasks] = useState<SavedTask[]>([]);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [savedEvents, setSavedEvents] = useState<ServerEvent[] | null>(null);
  const [failedOnly, setFailedOnly] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const response = await apiFetch("/api/v1/tasks" + (failedOnly ? "?failed=true" : ""), { signal: controller.signal });
        if (!response.ok) throw await responseProblem(response);
        const body = await response.json();
        if (controller.signal.aborted) return;
        setTasks(body.tasks); setError("");
        timer = setTimeout(() => void refresh(), 3000);
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "任务读取失败。");
      } finally { if (!controller.signal.aborted) setLoading(false); }
    }
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [revision, failedOnly]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setResult("正在读取保存的结果…");
    setSavedEvents(null);
    async function load() {
      let cursor = 0;
      const texts: string[] = [];
      const events: ServerEvent[] = [];
      try {
        while (true) {
          const response = await apiFetch(`/api/v1/tasks/${selected}/event-page?after=${cursor}`, { signal: controller.signal });
          if (!response.ok) throw await responseProblem(response);
          const body = await response.json();
          for (const row of body.events) {
            cursor = row.seq;
            events.push(row.event);
            if (row.event.type === "message") texts.push(row.event.content);
            if (row.event.type === "error") texts.push(row.event.message);
          }
          if (body.events.length < 200) break;
        }
        if (!controller.signal.aborted) {
          setResult(texts.join("\n\n") || "尚未生成最终结果；执行过程已保存，可稍后再次查看。");
          setSavedEvents(events);
        }
      } catch (reason) {
        if (!controller.signal.aborted) setResult(reason instanceof Error ? reason.message : "读取结果失败。");
      }
    }
    void load();
    return () => controller.abort();
  }, [selected, revision]);

  async function cancel(id: string) {
    try {
      const response = await apiFetch(`/api/v1/tasks/${id}/cancel`, { method: "POST" });
      if (!response.ok) throw await responseProblem(response);
      setRevision(value => value + 1);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "取消失败。"); }
  }

  async function review(id: string, disposition: "resolved" | "needs_support") {
    try {
      const response = await apiFetch(`/api/v1/tasks/${id}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ disposition }) });
      if (!response.ok) throw await responseProblem(response);
      setRevision(value => value + 1);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "处理记录保存失败。"); }
  }

  return <div className="task-list">
    <p>关闭页面不会取消已提交的任务。取消会停止后续执行，已经写入的记录不会回滚。</p>
    <button type="button" onClick={() => setRevision(value => value + 1)}>刷新任务</button>
    <label><input type="checkbox" checked={failedOnly} onChange={event => { setFailedOnly(event.target.checked); setSelected(null); }} />只看失败、超时、中断与取消</label>
    {error && <p role="alert" className="warning">{error}</p>}
    {loading && <p role="status">正在读取任务…</p>}
    {!loading && !tasks.length && <p>还没有已提交的任务。</p>}
    {tasks.map(task => <article className="saved-task" key={task.id}>
      <strong>{labels[task.status] ?? task.status}</strong>
      <time>{new Date(task.created_at).toLocaleString("zh-CN")}</time>
      <small>任务 {task.id.slice(0, 8)}</small>
      {["failed", "cancelled", "timed_out", "interrupted"].includes(task.status) && <div className="warning">
        <p>{task.health_committed === true ? "健康记录已保存，请先核对随访记录，勿直接重复提交。" : task.health_committed === false ? "未发现本任务的健康写入凭据，请核对执行结果。" : "无法确认健康记录持久化情况，请核对已有记录。"}</p>
        <p>处理状态：{task.review_disposition === "resolved" ? "已核对" : task.review_disposition === "needs_support" ? "已进入运维协助队列，等待处理。" : task.review_disposition === "support_resolved" ? "运维已处理。若问题仍在，可以再次标记。" : "待核对"}</p>
        <button disabled={task.review_disposition === "resolved"} onClick={() => void review(task.id, "resolved")}>已核对结果</button>
        <button disabled={task.review_disposition === "needs_support"} onClick={() => void review(task.id, "needs_support")}>标记需要人工协助</button>
      </div>}
      <button type="button" onClick={() => { setSelected(task.id); setRevision(value => value + 1); }}>查看保存结果</button>
      {["pending", "running"].includes(task.status) && <button type="button" disabled={task.cancel_requested}
        onClick={() => void cancel(task.id)}>{task.cancel_requested ? "正在取消…" : "取消任务"}</button>}
      {selected === task.id && <><p className="saved-result">{result}</p><small>请求编号：{task.request_id}</small>
        {["succeeded", "waiting_human"].includes(task.status) && <button type="button" disabled={busy || !savedEvents}
          onClick={() => savedEvents && onResume(task, savedEvents)}>继续此会话</button>}
      </>}
    </article>)}
  </div>;
}
