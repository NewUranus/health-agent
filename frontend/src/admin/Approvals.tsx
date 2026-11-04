import { useEffect, useState } from "react";

export type AdminApi = <T>(path: string, init?: RequestInit) => Promise<T>;
export type Approval = {
  id: string; fingerprint: string; action: "publish" | "delete"; document_id: number; status: string;
  expires_at: number; request_id: string; error_code: string | null;
  summary: { title: string; version: string; chunk_count: number; risk: string; replaced: { id: number; title: string; version: string }[] };
  result: unknown;
};
export class AdminProblem extends Error {
  constructor(message: string, public code?: string) { super(message); }
}
const labels: Record<string, string> = { waiting_human: "等待确认", executing: "执行中", succeeded: "已完成", rejected: "已拒绝",
  expired: "已过期", invalidated: "内容或权限已变化", failed: "执行失败", uncertain: "执行结果待核对" };

export function ApprovalDialog({ approval, api, onDone, onClose }: {
  approval: Approval; api: AdminApi; onDone: (result: Approval) => Promise<void>; onClose: () => void;
}) {
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  const expired = now >= approval.expires_at;
  async function decide(decision: "approve" | "reject") {
    setBusy(true); setError("");
    try {
      const result = await api<Approval>(`/approvals/${approval.id}/decision`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, fingerprint: approval.fingerprint }) });
      await onDone(result);
    } catch (reason) {
      setError((reason instanceof Error ? reason.message : "操作未完成。") + " 可到确认记录查看结果；重复提交此确认不会重复执行。");
    } finally { setBusy(false); }
  }
  return <div className="admin-modal-backdrop"><section role="dialog" aria-modal="true" aria-labelledby="approval-title" className="admin-modal">
    <h2 id="approval-title">{approval.action === "publish" ? "确认发布知识" : "确认删除文档"}</h2>
    <p><strong>{approval.summary.title}</strong> · {approval.summary.version || "未标版本"} · {approval.summary.chunk_count} 个片段</p>
    <p className="admin-review">{approval.summary.risk}</p>
    {!!approval.summary.replaced.length && <><h3>将替换并删除的旧文档</h3><ul>{approval.summary.replaced.map(item =>
      <li key={item.id}>{item.title} · {item.version || "未标版本"}（编号 {item.id}）</li>)}</ul></>}
    <p>有效期至 {new Date(approval.expires_at).toLocaleString("zh-CN")}；内容变化后必须重新核对。</p>
    <label><input type="checkbox" checked={checked} disabled={busy || expired} onChange={event => setChecked(event.target.checked)} />我已核对内容和影响范围</label>
    {expired && <p role="status">确认已过期，请关闭后重新发起。</p>}
    {error && <p role="alert" className="admin-error">{error}</p>}
    <div className="admin-actions">
      <button disabled={busy || expired || !checked} className="admin-primary" onClick={() => void decide("approve")}>{busy ? "处理中…" : "确认并执行"}</button>
      <button disabled={busy || expired} onClick={() => void decide("reject")}>拒绝操作</button>
      <button disabled={busy} onClick={onClose}>关闭</button>
    </div>
    <small>当前为管理员单级确认，关闭窗口不会批准操作。</small>
  </section></div>;
}

export function ApprovalHistory({ api, onChanged }: { api: AdminApi; onChanged: () => Promise<void> }) {
  const [rows, setRows] = useState<Approval[]>([]);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Approval | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void api<{ approvals: Approval[] }>("/approvals", { signal: controller.signal }).then(body => { if (!controller.signal.aborted) { setRows(body.approvals); setError(""); } })
      .catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [api, revision]);
  return <div className="admin-panel"><h2>确认记录</h2><p>这里记录知识发布和删除的处理结果。执行结果不明时先核对文档，不自动重放。</p>
    <button onClick={() => setRevision(value => value + 1)}>刷新确认记录</button>
    {error && <p role="alert">{error}</p>}
    {!rows.length && !error && <p>暂无确认记录。</p>}
    <ul className="admin-list">{rows.map(row => <li key={row.id}><strong>{row.summary.title}</strong> · {row.action === "publish" ? "发布" : "删除"} · {labels[row.status] ?? row.status}
      <p>有效期：{new Date(row.expires_at).toLocaleString("zh-CN")} · 请求编号：{row.request_id}</p>
      {row.error_code && <p>{row.error_code}：请核对文档现状，再决定是否发起新的确认。</p>}
      {row.status === "waiting_human" && <button onClick={() => setSelected(row)}>核对确认单</button>}
    </li>)}</ul>
    {selected && <ApprovalDialog key={selected.id} approval={selected} api={api} onClose={() => setSelected(null)} onDone={async () => {
      setSelected(null); setRevision(value => value + 1); await onChanged();
    }} />}
  </div>;
}
