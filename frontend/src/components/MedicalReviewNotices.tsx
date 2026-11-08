import { useEffect, useState } from "react";
import { apiFetch } from "../lib/api";

type Notice = {
  id: string; status: "approved" | "changes_requested"; doctor_note: string;
  reviewed_at: number | null; risk_percent: number | null; reasons: string[];
};

export function MedicalReviewNotices({ refreshKey }: { refreshKey: string }) {
  const [notices, setNotices] = useState<Notice[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    apiFetch("/api/v1/review-notices", { signal: controller.signal })
      .then(response => response.ok ? response.json() : Promise.reject())
      .then(body => { if (!controller.signal.aborted) setNotices(body.reviews ?? []); })
      .catch(() => { /* 审核提示读取失败不阻断健康对话。 */ });
    return () => controller.abort();
  }, [refreshKey]);
  if (!notices.length) return null;
  return <section className="medical-review-notices" aria-label="医学审核意见">
    {notices.slice(0, 3).map(notice => <div key={notice.id} className={notice.status === "approved" ? "success" : "warning"} role="status">
      <strong>{notice.status === "approved" ? "医学审核员已查看本次评估" : "医学审核员建议进一步核对"}</strong>
      {notice.doctor_note && <p>{notice.doctor_note}</p>}
      {notice.reviewed_at && <small>{new Date(notice.reviewed_at * 1000).toLocaleString("zh-CN")}</small>}
    </div>)}
  </section>;
}
