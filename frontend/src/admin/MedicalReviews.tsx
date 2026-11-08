import { useCallback, useEffect, useState } from "react";

type Api = <T>(path: string, init?: RequestInit) => Promise<T>;
type Review = {
  id: string; task_id: string; username: string; question: string; reply: string;
  risk_level: string; risk_percent: number | null; reasons: string[]; reason_labels: string[];
  status: "pending" | "approved" | "changes_requested"; doctor_note: string;
  created_at: number; reviewed_at: number | null;
};

export function MedicalReviews({ api }: { api: Api }) {
  const [reviews, setReviews] = useState<Review[] | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const load = useCallback(async () => {
    const body = await api<{ reviews: Review[] }>("/medical-reviews?status=pending");
    setReviews(body.reviews);
  }, [api]);
  useEffect(() => { void load().catch(reason => setError(reason instanceof Error ? reason.message : "审核队列读取失败。")); }, [load]);

  async function decide(review: Review, decision: "approve" | "request_changes") {
    setBusy(review.id); setError("");
    try {
      await api(`/medical-reviews/${review.id}/decision`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, note: notes[review.id] ?? "" }),
      });
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "审核决定未保存。");
    } finally { setBusy(""); }
  }

  return <section className="admin-panel">
    <h2>医学审核</h2>
    <p>高风险评估、模型补充识别的急症，以及删除过无依据断言的回答会自动进入这里。审核意见会展示给对应用户。</p>
    {error && <p role="alert" className="admin-error">{error}</p>}
    {!reviews && !error && <p role="status">正在读取审核队列…</p>}
    {reviews?.length === 0 && <p>暂无待审核内容。</p>}
    <ul className="admin-list">{reviews?.map(review => <li key={review.id}>
      <div className="admin-row"><strong>{review.username}</strong><span>{new Date(review.created_at * 1000).toLocaleString("zh-CN")}</span></div>
      <p className="warning">{review.reason_labels.join("；")}</p>
      {review.risk_percent != null && <p>风险：{review.risk_percent}%（{review.risk_level || "未分级"}）</p>}
      <details><summary>查看用户问题与系统回复</summary><h3>用户问题</h3><p className="admin-text">{review.question}</p><h3>系统回复</h3><p className="admin-text">{review.reply}</p></details>
      <label>审核意见<textarea maxLength={4000} value={notes[review.id] ?? ""} onChange={event => setNotes(current => ({ ...current, [review.id]: event.target.value }))} /></label>
      <div className="admin-actions"><button className="admin-primary" disabled={!!busy} onClick={() => void decide(review, "approve")}>通过</button>
        <button disabled={!!busy || !(notes[review.id] ?? "").trim()} onClick={() => void decide(review, "request_changes")}>需修改</button></div>
    </li>)}</ul>
  </section>;
}
